"""Host-managed UEX report and Blender export jobs for unpacked assets.

Provider locations are process-owner configuration.  MCP callers select only a
profile, an already indexed report specification, exact virtual assets, and a
fresh operation nonce; they cannot supply executables, profile databases, or
output paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence


JOB_SCHEMA = "renderdoc-ultra-unpacked-asset-job/v1"
JOB_VERSION = 1
CAPABILITIES_SCHEMA = "renderdoc-ultra-unpacked-asset-providers/v1"
DECISION_DISCOVERY_SCHEMA = "renderdoc-ultra-uex-decision-discovery/v1"
INVENTORY_ASSET_QUERY_SCHEMA = "renderdoc-ultra-uex-inventory-assets/v1"
BLENDER_ASSEMBLY_RECEIPT_SCHEMA = "renderdoc-ultra-uex-blender-assembly-receipt/v1"
BLENDER_CONTINUATION_PLAN_SCHEMA = "renderdoc-ultra-uex-blender-continuation-plan/v1"
BLENDER_CONTINUATION_KIND = "uex-blender-continuation"
JOB_ROOT_ENV = "RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT"
UEX_ENV = "RENDERDOC_ULTRA_UEX"
UEX_PROFILES_ENV = "RENDERDOC_ULTRA_UEX_PROFILES"
BLENDER_ENV = "RENDERDOC_ULTRA_BLENDER"
UEX_BLENDER_WRAPPER_ENV = "RENDERDOC_ULTRA_UEX_BLENDER_WRAPPER"
UEX_BLENDER_CONTINUATION_DRIVER_ENV = (
    "RENDERDOC_ULTRA_UEX_BLENDER_CONTINUATION_DRIVER"
)
_FROZEN_CONTINUATION_DRIVER_RELATIVE = (
    "replaylab_helpers",
    "unpacked_asset_jobs.py",
)
UEFORMAT_ADDON_ENV = "RENDERDOC_ULTRA_UEFORMAT_ADDON"
UEX_PYTHON_ENV = "RENDERDOC_ULTRA_UEX_PYTHON"

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID_RE = re.compile(r"^asset-v1-[0-9a-f]{64}$")
_PROFILE_RE = re.compile(r"^[^\x00\r\n]{1,256}$")
_REJECTED_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})
_MAX_ASSETS = 64
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_CONFIGURED_PROFILES = 512
_MAX_RETURNED_PROFILES = 64
_MAX_INVENTORIES = 100
_MAX_INVENTORY_METADATA_BYTES = 64 * 1024
_MAX_ASSET_QUERY_ROWS = 200
_MAX_ASSET_QUERY_OFFSET = 1_000_000
_MAX_ASSET_SEARCH_CHARS = 256
_MAX_ASSET_METRICS = 32
_MAX_REPORT_SPEC_BYTES = 512 * 1024
_MAX_REPORT_RULES = 32
_MAX_REPORT_REGEX_TIMEOUT_BUDGET_MS = 10_000
_MAX_REPORT_GLOBS = 256
_MAX_REPORT_CATEGORIES = 64
_MAX_REPORT_FRAME_BINS = 128
_MAX_REPORT_STRING_CHARS = 2048
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_LOG_TAIL_BYTES = 32 * 1024
_SUPPORTED_UEX_INVENTORY_SCHEMA = 2


class UnpackedAssetJobError(RuntimeError):
    """A provider capability, execution, or output-contract failure."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path, maximum: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if maximum is not None and total > maximum:
                raise UnpackedAssetJobError("provider artifact exceeds its fixed byte bound")
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(payload) > _MAX_STATE_BYTES:
        raise UnpackedAssetJobError("unpacked-asset job state exceeds its byte bound")
    temporary = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def _read_json(path: Path, maximum: int = _MAX_MANIFEST_BYTES) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UnpackedAssetJobError("provider JSON artifact is missing or redirected")
    if path.stat().st_size > maximum:
        raise UnpackedAssetJobError("provider JSON artifact exceeds its byte bound")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UnpackedAssetJobError("provider JSON artifact is invalid") from error
    if not isinstance(value, dict):
        raise UnpackedAssetJobError("provider JSON artifact must be an object")
    return value


def _strict_json_object(path: Path, maximum: int) -> dict[str, Any]:
    """Read a bounded JSON object while rejecting ambiguous duplicate keys."""

    if path.is_symlink() or not path.is_file():
        raise UnpackedAssetJobError("configured UEX metadata is unavailable")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise UnpackedAssetJobError("configured UEX metadata is unavailable") from error
    if not 1 <= size <= maximum:
        raise UnpackedAssetJobError("configured UEX metadata is outside its byte bound")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"), object_pairs_hook=no_duplicates
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise UnpackedAssetJobError("configured UEX metadata is invalid") from error
    if not isinstance(value, dict):
        raise UnpackedAssetJobError("configured UEX metadata must be an object")
    return value


def _is_redirected(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _bounded_text(value: Any, maximum: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > maximum or any(ord(character) < 32 for character in text):
        return ""
    return text


def _metadata_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _normalize_export_asset_path(value: Any) -> str:
    """Return the exact package-shaped path accepted by UEX AssetOps.

    UEX inventories commonly store object identities such as
    ``Game/Content/SK_Hero.SK_Hero`` while ``export-blender`` resolves a package
    key.  Only a same-name object suffix may be removed; other dotted suffixes
    are ambiguous and fail closed.
    """

    if not isinstance(value, str):
        raise UnpackedAssetJobError("virtual asset path must be a string")
    normalized = value.strip().replace("\\", "/").strip("/")
    if (
        not normalized
        or len(normalized) > 2048
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise UnpackedAssetJobError("virtual asset path is invalid")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnpackedAssetJobError("virtual asset path contains an unsafe component")

    leaf = parts[-1]
    for extension in (".uasset", ".umap"):
        if leaf.casefold().endswith(extension):
            leaf = leaf[: -len(extension)]
            break
    if "." in leaf:
        package_name, object_name = leaf.rsplit(".", 1)
        if not package_name or object_name.casefold() != package_name.casefold():
            raise UnpackedAssetJobError(
                "virtual asset object suffix is ambiguous; use the exact package identity"
            )
        leaf = package_name
    if not leaf:
        raise UnpackedAssetJobError("virtual asset package identity is empty")
    parts[-1] = leaf
    return "/".join(parts)


def _inventory_asset_identity_receipt(
    *, inventory_path: str, export_asset_path: str, asset_class: str, kind: str
) -> dict[str, Any]:
    receipt = {
        "inventoryPath": inventory_path,
        "exportAssetPath": export_asset_path,
        "assetClass": asset_class,
        "kind": kind,
        "available": True,
        "hasProviderError": False,
    }
    receipt["identitySha256"] = hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
    return receipt


def _safe_asset_metrics(value: Any) -> dict[str, float]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 64 * 1024:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    result: dict[str, float] = {}
    for key in sorted(parsed, key=lambda item: str(item).casefold()):
        if len(result) >= _MAX_ASSET_METRICS:
            break
        name = _bounded_text(key, 128)
        metric = parsed[key]
        if (
            not name
            or isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
        ):
            continue
        result[name] = float(metric)
    return result


def _report_spec_object(
    value: Any, *, label: str, allowed: frozenset[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnpackedAssetJobError("%s must be an object" % label)
    keys = set(value)
    if any(not isinstance(key, str) for key in keys) or not keys.issubset(allowed):
        raise UnpackedAssetJobError("%s contains unsupported fields" % label)
    return value


def _report_spec_string(
    value: Any, *, label: str, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise UnpackedAssetJobError("%s must be a string" % label)
    text = value.strip()
    if (
        len(text) > _MAX_REPORT_STRING_CHARS
        or any(ord(character) < 32 for character in text)
        or (not allow_empty and not text)
    ):
        raise UnpackedAssetJobError("%s is empty or outside its fixed bound" % label)
    return text


def _report_spec_globs(value: Any, *, label: str, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_REPORT_GLOBS:
        raise UnpackedAssetJobError("%s must be a bounded list" % label)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        glob = _report_spec_string(item, label="%s[%d]" % (label, index))
        glob = glob.replace("\\", "/").strip("/")
        if not glob:
            raise UnpackedAssetJobError("%s[%d] is empty" % (label, index))
        folded = glob.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(glob)
    if required and not result:
        raise UnpackedAssetJobError("%s requires at least one project rule" % label)
    return result


_DEFAULT_REPORT_FRAME_BINS: tuple[tuple[str, int, int | None], ...] = (
    ("0-30", 0, 31),
    ("31-60", 31, 61),
    ("61-120", 61, 121),
    ("121-300", 121, 301),
    ("301-600", 301, 601),
    ("601+", 601, None),
)


def normalize_asset_report_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return one bounded canonical UEX v1 grouping specification.

    This is deliberately a structural, fail-closed Host gate. UEX remains the
    authority for .NET regular-expression and template semantics when it loads
    the staged file, but the Host refuses missing project rules, unknown
    fields, unbounded collections, invalid scalar types, and non-finite values
    before it creates an operation directory.
    """

    source = _report_spec_object(
        value,
        label="report_spec",
        allowed=frozenset(
            {
                "version",
                "name",
                "characterPathRules",
                "excludedAssemblyGlobs",
                "primaryAssemblyGlobs",
                "scene",
                "animation",
            }
        ),
    )
    if isinstance(source.get("version"), bool) or source.get("version") != 1:
        raise UnpackedAssetJobError("report_spec.version must be 1")
    name = _report_spec_string(source.get("name"), label="report_spec.name")

    raw_rules = source.get("characterPathRules")
    if (
        not isinstance(raw_rules, list)
        or not raw_rules
        or len(raw_rules) > _MAX_REPORT_RULES
    ):
        raise UnpackedAssetJobError(
            "report_spec.characterPathRules requires bounded project grouping rules"
        )
    rules: list[dict[str, Any]] = []
    rule_names: set[str] = set()
    timeout_budget = 0
    template_defaults = {
        "category": "{category}",
        "character": "{character}",
        "variant": "{variant}",
        "state": "{state}",
        "component": "{component}",
        "logicalId": "{category}/{character}",
        "assemblyId": "{category}/{character}/{variant}/{state}",
    }
    for index, raw_rule in enumerate(raw_rules):
        prefix = "report_spec.characterPathRules[%d]" % index
        rule = _report_spec_object(
            raw_rule,
            label=prefix,
            allowed=frozenset(
                {"name", "regex", "ignoreCase", "matchTimeoutMilliseconds", "templates"}
            ),
        )
        rule_name = _report_spec_string(rule.get("name"), label=prefix + ".name")
        if rule_name.casefold() in rule_names:
            raise UnpackedAssetJobError("report_spec character rule names must be unique")
        rule_names.add(rule_name.casefold())
        expression = _report_spec_string(rule.get("regex"), label=prefix + ".regex")
        ignore_case = rule.get("ignoreCase", True)
        if not isinstance(ignore_case, bool):
            raise UnpackedAssetJobError(prefix + ".ignoreCase must be a boolean")
        timeout = rule.get("matchTimeoutMilliseconds", 250)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 10 <= timeout <= 10_000
        ):
            raise UnpackedAssetJobError(
                prefix + ".matchTimeoutMilliseconds must be from 10 to 10000"
            )
        timeout_budget += timeout
        if timeout_budget > _MAX_REPORT_REGEX_TIMEOUT_BUDGET_MS:
            raise UnpackedAssetJobError(
                "report_spec character-rule regex timeout budget exceeds 10000 ms"
            )
        raw_templates = rule.get("templates", {})
        templates = _report_spec_object(
            raw_templates,
            label=prefix + ".templates",
            allowed=frozenset(template_defaults),
        )
        normalized_templates: dict[str, str] = {}
        captures = {
            match.group(1)
            for match in re.finditer(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", expression)
        }
        captures.update(
            match.group(1)
            for match in re.finditer(r"\(\?'([A-Za-z_][A-Za-z0-9_]*)'", expression)
        )
        derived_order = {field: order for order, field in enumerate(template_defaults)}
        token_pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
        for field_index, (field, default) in enumerate(template_defaults.items()):
            template = _report_spec_string(
                templates.get(field, default),
                label=prefix + ".templates." + field,
                allow_empty=field == "variant",
            )
            for token in token_pattern.findall(template):
                dependency = derived_order.get(token)
                if token not in captures and (
                    dependency is None or dependency >= field_index
                ):
                    raise UnpackedAssetJobError(
                        prefix
                        + ".templates."
                        + field
                        + " references an unavailable capture or derived field"
                    )
            normalized_templates[field] = template
        rules.append(
            {
                "name": rule_name,
                "regex": expression,
                "ignoreCase": ignore_case,
                "matchTimeoutMilliseconds": timeout,
                "templates": normalized_templates,
            }
        )

    scene_source = _report_spec_object(
        source.get("scene"),
        label="report_spec.scene",
        allowed=frozenset(
            {"scopeGlobs", "exclusionGlobs", "categories", "fallbackCategory", "percentile"}
        ),
    )
    scope_globs = _report_spec_globs(
        scene_source.get("scopeGlobs"),
        label="report_spec.scene.scopeGlobs",
        required=True,
    )
    exclusion_globs = _report_spec_globs(
        scene_source.get("exclusionGlobs", []),
        label="report_spec.scene.exclusionGlobs",
    )
    raw_categories = scene_source.get("categories", [])
    if not isinstance(raw_categories, list) or len(raw_categories) > _MAX_REPORT_CATEGORIES:
        raise UnpackedAssetJobError("report_spec.scene.categories must be a bounded list")
    categories: list[dict[str, Any]] = []
    category_names: set[str] = set()
    for index, raw_category in enumerate(raw_categories):
        prefix = "report_spec.scene.categories[%d]" % index
        category = _report_spec_object(
            raw_category,
            label=prefix,
            allowed=frozenset({"name", "includeGlobs", "excludeGlobs"}),
        )
        category_name = _report_spec_string(category.get("name"), label=prefix + ".name")
        if category_name.casefold() in category_names:
            raise UnpackedAssetJobError("report_spec scene category names must be unique")
        category_names.add(category_name.casefold())
        categories.append(
            {
                "name": category_name,
                "includeGlobs": _report_spec_globs(
                    category.get("includeGlobs"),
                    label=prefix + ".includeGlobs",
                    required=True,
                ),
                "excludeGlobs": _report_spec_globs(
                    category.get("excludeGlobs", []),
                    label=prefix + ".excludeGlobs",
                ),
            }
        )
    fallback = _report_spec_string(
        scene_source.get("fallbackCategory", "Other"),
        label="report_spec.scene.fallbackCategory",
    )
    percentile = scene_source.get("percentile", 0.90)
    if (
        isinstance(percentile, bool)
        or not isinstance(percentile, (int, float))
        or not math.isfinite(float(percentile))
        or not 0 < float(percentile) < 1
    ):
        raise UnpackedAssetJobError(
            "report_spec.scene.percentile must be finite and between 0 and 1"
        )

    animation_source = _report_spec_object(
        source.get("animation", {}),
        label="report_spec.animation",
        allowed=frozenset({"coreMarker", "frameBins"}),
    )
    core_marker = _report_spec_string(
        animation_source.get("coreMarker", "Core"),
        label="report_spec.animation.coreMarker",
    ).replace("\\", "/").strip("/")
    if not core_marker:
        raise UnpackedAssetJobError("report_spec.animation.coreMarker is empty")
    default_bins = [
        {
            "label": label,
            "minimumInclusive": minimum,
            "maximumExclusive": maximum,
        }
        for label, minimum, maximum in _DEFAULT_REPORT_FRAME_BINS
    ]
    raw_bins = animation_source.get("frameBins", default_bins)
    if (
        not isinstance(raw_bins, list)
        or not raw_bins
        or len(raw_bins) > _MAX_REPORT_FRAME_BINS
    ):
        raise UnpackedAssetJobError("report_spec.animation.frameBins must be a bounded list")
    frame_bins: list[dict[str, Any]] = []
    labels: set[str] = set()
    expected_minimum: int | None = 0
    for index, raw_bin in enumerate(raw_bins):
        prefix = "report_spec.animation.frameBins[%d]" % index
        frame_bin = _report_spec_object(
            raw_bin,
            label=prefix,
            allowed=frozenset({"label", "minimumInclusive", "maximumExclusive"}),
        )
        label = _report_spec_string(frame_bin.get("label"), label=prefix + ".label")
        if label.casefold() in labels:
            raise UnpackedAssetJobError("report_spec animation frame labels must be unique")
        labels.add(label.casefold())
        minimum = frame_bin.get("minimumInclusive")
        maximum = frame_bin.get("maximumExclusive")
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or not 0 <= minimum <= 2_147_483_647
            or minimum != expected_minimum
        ):
            raise UnpackedAssetJobError(
                prefix + ".minimumInclusive does not form a contiguous non-negative range"
            )
        if maximum is not None and (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not minimum < maximum <= 2_147_483_647
        ):
            raise UnpackedAssetJobError(prefix + ".maximumExclusive is invalid")
        if maximum is None and index != len(raw_bins) - 1:
            raise UnpackedAssetJobError("only the final report frame bin may be open-ended")
        frame_bins.append(
            {
                "label": label,
                "minimumInclusive": minimum,
                "maximumExclusive": maximum,
            }
        )
        expected_minimum = maximum
    if frame_bins[-1]["maximumExclusive"] is not None:
        raise UnpackedAssetJobError("the final report frame bin must be open-ended")

    normalized = {
        "version": 1,
        "name": name,
        "characterPathRules": rules,
        "excludedAssemblyGlobs": _report_spec_globs(
            source.get("excludedAssemblyGlobs", []),
            label="report_spec.excludedAssemblyGlobs",
        ),
        "primaryAssemblyGlobs": _report_spec_globs(
            source.get("primaryAssemblyGlobs", []),
            label="report_spec.primaryAssemblyGlobs",
        ),
        "scene": {
            "scopeGlobs": scope_globs,
            "exclusionGlobs": exclusion_globs,
            "categories": categories,
            "fallbackCategory": fallback,
            "percentile": float(percentile),
        },
        "animation": {"coreMarker": core_marker, "frameBins": frame_bins},
    }
    if len(_canonical_bytes(normalized)) > _MAX_REPORT_SPEC_BYTES:
        raise UnpackedAssetJobError("report_spec exceeds its fixed canonical byte bound")
    return normalized


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _managed_file(root: Path, value: Any, label: str) -> Path:
    """Resolve one regular, non-redirected provider artifact below ``root``."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise UnpackedAssetJobError("%s path is invalid" % label)
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise UnpackedAssetJobError("%s file is missing" % label) from error
    cursor = candidate
    while True:
        if cursor.is_symlink():
            raise UnpackedAssetJobError("%s path is redirected" % label)
        if cursor == root or cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not candidate.is_file() or not _within(resolved_root, resolved):
        raise UnpackedAssetJobError("%s escaped its managed output" % label)
    return resolved


def _bundle_relative_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UnpackedAssetJobError("%s must be a relative file" % label)
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnpackedAssetJobError("%s must be relative" % label)
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnpackedAssetJobError("%s contains an unsafe path component" % label)
    return _managed_file(root, normalized, label)


def _configured_file(environment: Mapping[str, str], key: str, fallback: str = "") -> Path | None:
    value = str(environment.get(key, "") or "").strip()
    if not value and fallback:
        value = str(environment.get(fallback, "") or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        if not path.is_absolute() or _is_redirected(path) or not path.is_file():
            return None
        return path.resolve(strict=True)
    except OSError:
        return None


def _bundled_continuation_driver() -> Path | None:
    """Resolve the fixed PyInstaller data helper without consulting cwd/PATH.

    A onefile build imports this module from its embedded PYZ, which is not a
    source path an independently configured Python interpreter can execute.
    Packaging therefore adds the audited source module at one fixed location
    below PyInstaller's process-owned extraction root.  Treat any missing,
    relative, redirected, or escaping path as unavailable.
    """

    if not bool(getattr(sys, "frozen", False)):
        return None
    raw_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(raw_root, (str, os.PathLike)):
        return None
    root = Path(raw_root)
    if not root.is_absolute():
        return None
    candidate = root.joinpath(*_FROZEN_CONTINUATION_DRIVER_RELATIVE)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not root.is_dir() or not _within(resolved_root, resolved):
        return None
    cursor = candidate
    while True:
        if _is_redirected(cursor):
            return None
        if cursor == root:
            break
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent
    if not candidate.is_file():
        return None
    return resolved


def _continuation_driver(environment: Mapping[str, str]) -> Path | None:
    """Resolve the process-owner override, frozen helper, or source fallback."""

    configured = str(
        environment.get(UEX_BLENDER_CONTINUATION_DRIVER_ENV, "") or ""
    ).strip()
    if configured:
        # An explicit process-owner value is authoritative.  If it is invalid,
        # fail closed rather than silently changing the provider identity.
        return _configured_file(environment, UEX_BLENDER_CONTINUATION_DRIVER_ENV)
    if bool(getattr(sys, "frozen", False)):
        return _bundled_continuation_driver()
    candidate = Path(__file__)
    try:
        if (
            not candidate.is_absolute()
            or _is_redirected(candidate)
            or not candidate.is_file()
        ):
            return None
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _configured_executable(
    environment: Mapping[str, str], key: str, fallback_env: str, command: str
) -> Path | None:
    configured = _configured_file(environment, key, fallback_env)
    if configured is None:
        found = shutil.which(command, path=str(environment.get("PATH", "") or ""))
        if found:
            candidate = Path(found)
            try:
                if (
                    candidate.is_absolute()
                    and not _is_redirected(candidate)
                    and candidate.is_file()
                ):
                    configured = candidate.resolve(strict=True)
            except OSError:
                configured = None
    if configured is None or configured.suffix.casefold() in _REJECTED_EXECUTABLE_SUFFIXES:
        return None
    return configured


def _provider_fingerprint(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "sha256": "", "name": ""}
    return {
        "available": True,
        "sha256": _sha256_file(path),
        "name": path.name,
    }


def _default_root(environment: Mapping[str, str]) -> Path:
    configured = str(environment.get(JOB_ROOT_ENV, "") or "").strip()
    if configured:
        raw = Path(configured).expanduser()
        if not raw.is_absolute():
            raise UnpackedAssetJobError("unpacked-asset job root must be absolute")
        return raw
    local = str(environment.get("LOCALAPPDATA", "") or "").strip()
    base = Path(local) if local else Path.home() / ".renderdoc-ultra"
    return base / "ReplayLab" / "unpacked-asset-jobs"


def _redact(text: str) -> str:
    value = re.sub(
        r"(?i)\b(aes(?:[_ -]?key)?|api[_ -]?key|password|secret|token)(\s*[:=]\s*)([^\s,;]+)",
        lambda match: "%s%s<redacted>" % (match.group(1), match.group(2)),
        text,
    )
    return re.sub(r"(?i)\b[0-9a-f]{48,}\b", "<redacted-hex>", value)


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            data = handle.read(_LOG_TAIL_BYTES)
        return _redact(data.decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _safe_environment(environment: Mapping[str, str], job_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"):
        value = str(environment.get(key, "") or "")
        if value:
            result[key] = value
    result["PYTHONUTF8"] = "1"
    result["RENDERDOC_ULTRA_PROVIDER_OUTPUT"] = str(job_dir)
    return result


def _continuation_provider_file(record: Any, label: str) -> Path:
    if not isinstance(record, Mapping):
        raise UnpackedAssetJobError("continuation %s provider binding is invalid" % label)
    value = record.get("path")
    expected = record.get("sha256")
    if (
        not isinstance(value, str)
        or not value
        or not isinstance(expected, str)
        or not _SHA256_RE.fullmatch(expected)
    ):
        raise UnpackedAssetJobError("continuation %s provider binding is invalid" % label)
    path = Path(value)
    try:
        if (
            not path.is_absolute()
            or _is_redirected(path)
            or not path.is_file()
            or _sha256_file(path) != expected
        ):
            raise UnpackedAssetJobError(
                "continuation %s provider changed after dispatch" % label
            )
        return path.resolve(strict=True)
    except OSError as error:
        raise UnpackedAssetJobError(
            "continuation %s provider is unavailable" % label
        ) from error


def _continuation_source_root(managed_root: Path, record: Mapping[str, Any]) -> Path:
    value = record.get("sourceRoot")
    operation_id = record.get("sourceOperationId")
    if (
        not isinstance(value, str)
        or not isinstance(operation_id, str)
        or not _HEX32_RE.fullmatch(operation_id)
    ):
        raise UnpackedAssetJobError("continuation retained source binding is invalid")
    expected = managed_root / "jobs" / operation_id / "target"
    try:
        if (
            not Path(value).is_absolute()
            or _is_redirected(expected)
            or not expected.is_dir()
            or Path(value).resolve(strict=True) != expected.resolve(strict=True)
        ):
            raise UnpackedAssetJobError("continuation retained source escaped its job")
        return expected.resolve(strict=True)
    except OSError as error:
        raise UnpackedAssetJobError("continuation retained source is unavailable") from error


def _continuation_verify_artifacts(
    source_root: Path, records: Any
) -> dict[str, Path]:
    if not isinstance(records, list) or not records:
        raise UnpackedAssetJobError("continuation retained artifact set is empty")
    verified: dict[str, Path] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise UnpackedAssetJobError("continuation retained artifact is invalid")
        relative = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        if (
            not isinstance(relative, str)
            or relative in verified
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or not _SHA256_RE.fullmatch(sha256)
        ):
            raise UnpackedAssetJobError("continuation retained artifact receipt is invalid")
        path = _managed_file(source_root, relative, "continuation retained artifact")
        if path.stat().st_size != size or _sha256_file(path) != sha256:
            raise UnpackedAssetJobError("continuation retained artifact hash changed")
        verified[relative] = path
    return verified


def _run_continuation_command(command: Sequence[str], label: str) -> None:
    try:
        completed = subprocess.run(list(command), shell=False, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as error:
        raise UnpackedAssetJobError("%s could not start: %s" % (label, error)) from error
    if completed.returncode != 0:
        raise UnpackedAssetJobError(
            "%s failed with exit code %d" % (label, completed.returncode)
        )


def _continuation_bundle_record(
    asset: str, manifest_path: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    materials = manifest.get("materials")
    textures = manifest.get("textures")
    meshes = manifest.get("meshes")
    material_values = materials if isinstance(materials, list) else []
    texture_values = textures if isinstance(textures, list) else []
    mesh_values = meshes if isinstance(meshes, list) else []
    return {
        "asset": asset,
        "directory": str(manifest_path.parent),
        "manifest": str(manifest_path),
        "meshName": manifest.get("meshName"),
        "sourceLodCount": manifest.get("sourceLodCount"),
        "exportedLodCount": len(mesh_values),
        "boneCount": manifest.get("boneCount"),
        "morphTargetCount": manifest.get("morphTargetCount"),
        "materialCount": len(material_values),
        "boundBaseColorCount": sum(
            1
            for material in material_values
            if isinstance(material, Mapping) and material.get("baseColor")
        ),
        "textureCount": len(texture_values),
        "warningCount": len(manifest.get("warnings", []))
        if isinstance(manifest.get("warnings"), list)
        else 0,
        "hasUeModel": bool(manifest.get("ueModel")),
    }


def _run_blender_continuation_plan(plan_path: Path) -> None:
    """Run one Host-authored retry/assembly plan in a single owned process."""

    plan_path = plan_path.resolve(strict=True)
    plan = _strict_json_object(plan_path, _MAX_STATE_BYTES)
    if plan.get("schema") != BLENDER_CONTINUATION_PLAN_SCHEMA:
        raise UnpackedAssetJobError("Blender continuation plan schema is unsupported")
    operation_id = plan.get("operationId")
    if not isinstance(operation_id, str) or not _HEX32_RE.fullmatch(operation_id):
        raise UnpackedAssetJobError("Blender continuation operation binding is invalid")
    managed_root_value = plan.get("managedRoot")
    if not isinstance(managed_root_value, str) or not Path(managed_root_value).is_absolute():
        raise UnpackedAssetJobError("Blender continuation managed root is invalid")
    managed_root = Path(managed_root_value).resolve(strict=True)
    job_dir = managed_root / "jobs" / operation_id
    target = job_dir / "target"
    if (
        _is_redirected(managed_root)
        or _is_redirected(job_dir)
        or _is_redirected(target)
        or not target.is_dir()
        or plan_path != (job_dir / "input" / "continuation-plan.json").resolve(strict=True)
    ):
        raise UnpackedAssetJobError("Blender continuation plan escaped its managed job")

    providers = plan.get("providers")
    if not isinstance(providers, Mapping):
        raise UnpackedAssetJobError("Blender continuation provider set is invalid")
    python = _continuation_provider_file(providers.get("python"), "Python")
    wrapper = _continuation_provider_file(providers.get("wrapper"), "wrapper")
    uex = _continuation_provider_file(providers.get("uex"), "UEX")
    profiles = _continuation_provider_file(providers.get("profiles"), "profiles")
    driver = _continuation_provider_file(providers.get("driver"), "Host driver")
    if driver != Path(__file__).resolve(strict=True):
        raise UnpackedAssetJobError("Blender continuation Host driver identity changed")

    profile = plan.get("profile")
    full_assets = plan.get("sourceAssets")
    retry_assets = plan.get("retryAssets")
    sources = plan.get("sources")
    options = plan.get("options")
    if (
        not isinstance(profile, str)
        or not _PROFILE_RE.fullmatch(profile)
        or not isinstance(full_assets, list)
        or not isinstance(retry_assets, list)
        or not isinstance(sources, list)
        or len(sources) != len(full_assets)
        or not isinstance(options, Mapping)
    ):
        raise UnpackedAssetJobError("Blender continuation selection is invalid")
    full_assets = [_normalize_export_asset_path(asset) for asset in full_assets]
    retry_assets = [_normalize_export_asset_path(asset) for asset in retry_assets]
    if (
        not 1 <= len(full_assets) <= _MAX_ASSETS
        or len(set(asset.casefold() for asset in full_assets)) != len(full_assets)
        or len(set(asset.casefold() for asset in retry_assets)) != len(retry_assets)
        or any(asset not in full_assets for asset in retry_assets)
    ):
        raise UnpackedAssetJobError("Blender continuation asset set is invalid")
    for key in ("lod0Only", "exportMorphTargets", "preferUeModel", "exportOnly"):
        if not isinstance(options.get(key), bool):
            raise UnpackedAssetJobError("Blender continuation options are invalid")

    assembler: Path | None = None
    validator: Path | None = None
    if not options["exportOnly"]:
        assembler = _continuation_provider_file(
            providers.get("assembler"), "assembler"
        )
        validator = _continuation_provider_file(
            providers.get("validator"), "validator"
        )
        if assembler.parent != wrapper.parent or validator.parent != wrapper.parent:
            raise UnpackedAssetJobError(
                "Blender continuation scripts are not one fixed provider set"
            )

    addon: Path | None = None
    addon_value = plan.get("ueformatAddon")
    if options["preferUeModel"] and not options["exportOnly"]:
        if not isinstance(addon_value, str) or not addon_value:
            raise UnpackedAssetJobError("Blender continuation UEFormat provider is absent")
        candidate = Path(addon_value)
        if not candidate.is_absolute() or _is_redirected(candidate) or not candidate.exists():
            raise UnpackedAssetJobError("Blender continuation UEFormat provider is unavailable")
        addon = candidate.resolve(strict=True)

    retained_manifests: dict[int, Path] = {}
    retained_roots: dict[int, Path] = {}
    retry_positions: dict[str, int] = {
        asset: position for position, asset in enumerate(retry_assets)
    }
    for index, (asset, raw_source) in enumerate(zip(full_assets, sources)):
        if not isinstance(raw_source, Mapping) or raw_source.get("asset") != asset:
            raise UnpackedAssetJobError("Blender continuation source order changed")
        origin = raw_source.get("origin")
        if origin == "retained":
            source_root = _continuation_source_root(managed_root, raw_source)
            verified = _continuation_verify_artifacts(
                source_root, raw_source.get("artifacts")
            )
            manifest_record = raw_source.get("bundleManifest")
            if not isinstance(manifest_record, Mapping):
                raise UnpackedAssetJobError("retained bundle manifest binding is invalid")
            relative = manifest_record.get("path")
            if not isinstance(relative, str) or verified.get(relative) is None:
                raise UnpackedAssetJobError("retained bundle manifest is not in its artifact set")
            manifest_path = verified[relative]
            if (
                manifest_path.stat().st_size != manifest_record.get("size")
                or _sha256_file(manifest_path) != manifest_record.get("sha256")
            ):
                raise UnpackedAssetJobError("retained bundle manifest receipt changed")
            retained_manifests[index] = manifest_path
            retained_roots[index] = source_root
        elif origin == "retry":
            if asset not in retry_positions or raw_source.get("retryIndex") != retry_positions[asset]:
                raise UnpackedAssetJobError("Blender continuation retry order changed")
        else:
            raise UnpackedAssetJobError("Blender continuation source origin is invalid")

    retry_root = target / "retry"
    retry_blend = retry_root / "Retry_Assembly.blend"
    if retry_assets:
        command = [
            str(python),
            str(wrapper),
            "--uex",
            str(uex),
            "--config",
            str(profiles),
            "--profile",
            profile,
        ]
        for asset in retry_assets:
            command.extend(("--asset", asset))
        command.extend(("--out", str(retry_blend), "--export-only"))
        if options["lod0Only"]:
            command.append("--lod0-only")
        if not options["exportMorphTargets"]:
            command.append("--no-morph-targets")
        _run_continuation_command(command, "UEX continuation retry export")

    manifests: list[Path] = []
    manifest_values: list[dict[str, Any]] = []
    bundle_records: list[dict[str, Any]] = []
    preflight_state = {
        "request": {
            "inventoryId": 1,
            "inventoryDecisionSha256": "0" * 64,
            "inventoryAssets": [
                {"identitySha256": "%064x" % (index + 1)}
                for index in range(len(full_assets))
            ],
            "lod0Only": options["lod0Only"],
            "preferUeModel": options["preferUeModel"],
        }
    }
    preflight_manager = UnpackedAssetJobManager(managed_root, {})
    for index, asset in enumerate(full_assets):
        if index in retained_manifests:
            manifest_path = retained_manifests[index]
            bundle_root = retained_roots[index]
        else:
            retry_index = retry_positions[asset]
            manifest_path = (
                retry_root
                / "Retry_Assembly_uex_export"
                / "bundles"
                / UnpackedAssetJobManager._bundle_directory_name(asset, retry_index)
                / "manifest.json"
            )
            manifest_path = _managed_file(target, str(manifest_path), "retried bundle manifest")
            bundle_root = target
        manifest = _read_json(manifest_path)
        if (
            manifest.get("schema") != "uex.blender-bundle.v1"
            or _normalize_export_asset_path(manifest.get("sourceAsset")) != asset
        ):
            raise UnpackedAssetJobError("Blender continuation bundle identity changed")
        preflight_manager._verify_blender_bundle_item(
            state=preflight_state,
            target=bundle_root,
            asset=asset,
            index=index,
            bundle_record={
                "asset": asset,
                "manifest": str(manifest_path),
                "directory": str(manifest_path.parent),
                "hasUeModel": bool(manifest.get("ueModel")),
            },
        )
        manifests.append(manifest_path)
        manifest_values.append(manifest)
        bundle_records.append(_continuation_bundle_record(asset, manifest_path, manifest))

    blend = target / "UEX_Assembly.blend"
    assembly = blend.with_suffix(".assembly.json")
    validation = blend.with_suffix(".validation.json")
    export_only = bool(options["exportOnly"])
    geometry_source = "not-assembled" if export_only else (
        "uemodel" if options["preferUeModel"] else "glb"
    )
    if not export_only:
        blender = _continuation_provider_file(providers.get("blender"), "Blender")
        importer = manifests[0].parent / "import_to_blender.py"
        if _is_redirected(importer) or not importer.is_file():
            raise UnpackedAssetJobError("Blender continuation bundle importer is missing")
        command = [
            str(blender),
            "--background",
            "--factory-startup",
            "--python-exit-code",
            "1",
            "--python",
            str(assembler),
            "--",
            "--importer",
            str(importer.resolve(strict=True)),
        ]
        for manifest in manifests:
            command.extend(("--manifest", str(manifest)))
        command.extend(("--output", str(blend)))
        if addon is not None:
            command.extend(("--ueformat-addon", str(addon)))
        _run_continuation_command(command, "Blender continuation assembly")
        if _is_redirected(blend) or not blend.is_file():
            raise UnpackedAssetJobError("Blender continuation omitted its .blend output")
        _run_continuation_command(
            [
                str(blender),
                "--background",
                str(blend),
                "--python-exit-code",
                "1",
                "--python",
                str(validator),
                "--",
                "--report",
                str(validation),
            ],
            "Blender continuation validation",
        )
        validation_value = _read_json(validation)
        if (
            validation_value.get("schema") != "uex.blender-validation.v1"
            or validation_value.get("ok") is not True
        ):
            raise UnpackedAssetJobError("Blender continuation validation did not pass")

    parent = plan.get("parent")
    if not isinstance(parent, Mapping):
        raise UnpackedAssetJobError("Blender continuation parent binding is invalid")
    summary = {
        "schema": "uex.blender-assembly.v1",
        "status": "exported" if export_only else "complete",
        "profile": profile,
        "sourceAssets": full_assets,
        "bundles": bundle_records,
        "geometrySource": geometry_source,
        "outputBlend": None if export_only else str(blend.resolve(strict=True)),
        "outputSha256": None if export_only else _sha256_file(blend),
        "validationReport": (
            None if export_only else str(validation.resolve(strict=True))
        ),
        "validationOk": None if export_only else True,
        "assemblyManifest": str(assembly.resolve()),
        "options": {
            "lod0Only": options["lod0Only"],
            "exportMorphTargets": options["exportMorphTargets"],
            "overwrite": False,
            "exportOnly": export_only,
            "preferUeModel": options["preferUeModel"],
        },
        "continuation": {
            "parentOperationId": parent.get("operationId"),
            "parentRequestSha256": parent.get("requestSha256"),
            "parentResultSha256": parent.get("resultSha256"),
            "retainedBundleCount": len(retained_manifests),
            "retriedBundleCount": len(retry_assets),
        },
    }
    _atomic_json(assembly, summary)


@dataclass
class _RunningJob:
    process: subprocess.Popen[Any]
    stdout: Any
    stderr: Any


class UnpackedAssetJobManager:
    """Start and verify bounded UEX jobs below one process-owned root."""

    def __init__(self, root: str | Path, environment: Mapping[str, str] | None = None):
        self.root = Path(root).expanduser()
        self.environment = dict(os.environ if environment is None else environment)
        self._guard = threading.RLock()
        self._running: dict[str, _RunningJob] = {}

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "UnpackedAssetJobManager":
        values = dict(os.environ if environment is None else environment)
        return cls(_default_root(values), values)

    def _ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise UnpackedAssetJobError("unpacked-asset job root is redirected or unavailable")
        return self.root.resolve(strict=True)

    def _provider_paths(self) -> dict[str, Path | None]:
        uex = _configured_executable(self.environment, UEX_ENV, "UEX_EXE", "uex")
        profiles = _configured_file(
            self.environment, UEX_PROFILES_ENV, "UEX_PROFILES"
        )
        blender = _configured_executable(
            self.environment, BLENDER_ENV, "BLENDER_EXE", "blender"
        )
        wrapper = _configured_file(self.environment, UEX_BLENDER_WRAPPER_ENV)
        if wrapper is None:
            personal = (
                Path.home()
                / ".codex"
                / "skills"
                / "uex-export-blender"
                / "scripts"
                / "export_mesh_to_blender.py"
            )
            if personal.is_file() and not personal.is_symlink():
                wrapper = personal.resolve(strict=True)
        continuation_driver = _continuation_driver(self.environment)
        python = _configured_executable(
            self.environment, UEX_PYTHON_ENV, "PYTHON_EXECUTABLE", "python"
        )
        if python is None and not bool(getattr(sys, "frozen", False)):
            current_python = Path(sys.executable)
            if (
                current_python.is_absolute()
                and current_python.is_file()
                and not current_python.is_symlink()
            ):
                python = current_python.resolve(strict=True)
        assembler: Path | None = None
        validator: Path | None = None
        if wrapper is not None:
            assembler_candidate = wrapper.with_name("assemble_blender.py")
            validator_candidate = wrapper.with_name("validate_blend.py")
            if (
                assembler_candidate.is_file()
                and not _is_redirected(assembler_candidate)
            ):
                assembler = assembler_candidate.resolve(strict=True)
            if (
                validator_candidate.is_file()
                and not _is_redirected(validator_candidate)
            ):
                validator = validator_candidate.resolve(strict=True)
        addon_value = str(
            self.environment.get(UEFORMAT_ADDON_ENV, "")
            or self.environment.get("UEFORMAT_BLENDER_ADDON", "")
            or ""
        ).strip()
        addon: Path | None = None
        if addon_value:
            candidate = Path(addon_value).expanduser()
            if candidate.is_absolute() and candidate.exists() and not candidate.is_symlink():
                addon = candidate.resolve(strict=True)
        return {
            "uex": uex,
            "profiles": profiles,
            "blender": blender,
            "wrapper": wrapper,
            "python": python,
            "ueformatAddon": addon,
            "blenderAssembler": assembler,
            "blenderValidator": validator,
            "blenderContinuationDriver": continuation_driver,
        }

    def _configured_profiles(self) -> list[dict[str, Any]]:
        profiles_path = self._provider_paths()["profiles"]
        if profiles_path is None:
            raise UnpackedAssetJobError("UEX profiles are not configured")
        root = _strict_json_object(profiles_path, _MAX_CONFIG_BYTES)
        configured = root.get("profiles")
        if not isinstance(configured, Mapping) or not configured:
            raise UnpackedAssetJobError("UEX profiles metadata has no profiles")
        if len(configured) > _MAX_CONFIGURED_PROFILES:
            raise UnpackedAssetJobError("UEX profile enumeration exceeds its fixed bound")
        config_root = profiles_path.parent.resolve(strict=True)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for raw_name, raw_profile in configured.items():
            if (
                not isinstance(raw_name, str)
                or raw_name != raw_name.strip()
                or not _PROFILE_RE.fullmatch(raw_name)
            ):
                raise UnpackedAssetJobError("UEX profile name is invalid")
            folded = raw_name.casefold()
            if folded in seen:
                raise UnpackedAssetJobError("UEX profile names are ambiguous")
            seen.add(folded)
            database: Path | None = None
            if isinstance(raw_profile, Mapping):
                analysis_value = raw_profile.get("analysisDir")
                if analysis_value in (None, ""):
                    safe_name = "".join(
                        "_" if character in '/\\:*?\"<>|' else character
                        for character in raw_name
                    )
                    analysis = config_root / ".uex-analysis" / safe_name
                elif isinstance(analysis_value, str) and "\x00" not in analysis_value:
                    requested = Path(analysis_value).expanduser()
                    analysis = requested if requested.is_absolute() else config_root / requested
                else:
                    analysis = None
                if analysis is not None:
                    database = analysis / "inventory.db"
            result.append({"profile": raw_name, "database": database})
        return sorted(result, key=lambda item: str(item["profile"]).casefold())

    @staticmethod
    def _select_profile(
        profiles: Sequence[Mapping[str, Any]], profile: str
    ) -> Mapping[str, Any]:
        if not isinstance(profile, str) or profile != profile.strip() or not _PROFILE_RE.fullmatch(profile):
            raise UnpackedAssetJobError("profile is invalid")
        for candidate in profiles:
            if str(candidate.get("profile", "")).casefold() == profile.casefold():
                return candidate
        raise UnpackedAssetJobError("configured UEX profile was not found")

    @staticmethod
    def _open_inventory_database(profile: Mapping[str, Any]) -> sqlite3.Connection:
        database = profile.get("database")
        if not isinstance(database, Path):
            raise UnpackedAssetJobError("UEX profile has no valid Inventory store")
        analysis_root = database.parent
        try:
            if (
                _is_redirected(analysis_root)
                or _is_redirected(database)
                or not analysis_root.is_dir()
                or not database.is_file()
            ):
                raise UnpackedAssetJobError("UEX Inventory store is unavailable")
            resolved_root = analysis_root.resolve(strict=True)
            resolved_database = database.resolve(strict=True)
        except OSError as error:
            raise UnpackedAssetJobError("UEX Inventory store is unavailable") from error
        if resolved_database.parent != resolved_root:
            raise UnpackedAssetJobError("UEX Inventory store is redirected")
        try:
            connection = sqlite3.connect(
                resolved_database.as_uri() + "?mode=ro",
                uri=True,
                timeout=2.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON;")
            connection.execute("PRAGMA trusted_schema = OFF;")
            version = int(connection.execute("PRAGMA user_version;").fetchone()[0])
            if version != _SUPPORTED_UEX_INVENTORY_SCHEMA:
                raise UnpackedAssetJobError("UEX Inventory schema is unsupported")
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(inventories);").fetchall()
            }
            required = {
                "id",
                "profile",
                "generated_at",
                "stored_at",
                "analyzer_version",
                "metadata_json",
                "asset_count",
                "relation_count",
                "error_count",
            }
            if not required.issubset(columns):
                raise UnpackedAssetJobError("UEX Inventory schema is incomplete")
            return connection
        except Exception:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise

    @staticmethod
    def _inventory_receipt(row: sqlite3.Row, configured_profile: str) -> dict[str, Any]:
        inventory_id = row["id"]
        counts = (row["asset_count"], row["relation_count"], row["error_count"])
        if (
            isinstance(inventory_id, bool)
            or not isinstance(inventory_id, int)
            or inventory_id <= 0
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
            or str(row["profile"] or "").casefold() != configured_profile.casefold()
        ):
            raise UnpackedAssetJobError("UEX Inventory row is invalid")
        metadata_bytes = row["metadata_bytes"]
        if (
            isinstance(metadata_bytes, bool)
            or not isinstance(metadata_bytes, int)
            or metadata_bytes < 0
            or metadata_bytes > _MAX_INVENTORY_METADATA_BYTES
        ):
            metadata: Mapping[str, Any] = {}
            metadata_valid = False
        else:
            raw_metadata = row["metadata_json"]
            try:
                parsed = {} if raw_metadata in (None, "") else json.loads(raw_metadata)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            metadata_valid = isinstance(parsed, Mapping)
            metadata = parsed if isinstance(parsed, Mapping) else {}
        folded_metadata = {str(key).casefold(): value for key, value in metadata.items()}
        confidence = _bounded_text(folded_metadata.get("discoveryconfidence"), 32)
        truncated = _metadata_boolean(folded_metadata.get("truncated"))
        strong = confidence.casefold() == "strong"
        complete = bool(metadata_valid and strong and truncated is False)
        receipt = {
            "inventoryId": inventory_id,
            "profile": configured_profile,
            "generatedAt": _bounded_text(row["generated_at"], 64),
            "storedAt": _bounded_text(row["stored_at"], 64),
            "assetCount": counts[0],
            "relationCount": counts[1],
            "errorCount": counts[2],
            "discoveryConfidence": confidence or "unknown",
            "strong": strong,
            "committed": True,
            "complete": complete,
            "truncated": truncated,
            "reportEligible": complete,
            "status": "complete" if complete else "partial",
            "metadataRecognized": bool(metadata_valid and confidence and truncated is not None),
        }
        receipt["decisionSha256"] = hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
        return receipt

    def _inventory_records(
        self,
        profile: Mapping[str, Any],
        *,
        limit: int,
        inventory_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        connection = self._open_inventory_database(profile)
        configured_profile = str(profile["profile"])
        try:
            if inventory_id is None:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM inventories WHERE profile = ? COLLATE NOCASE;",
                        (configured_profile,),
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    """
                    SELECT id, profile, generated_at, stored_at, analyzer_version,
                           metadata_json, asset_count, relation_count, error_count,
                           length(CAST(metadata_json AS BLOB)) AS metadata_bytes
                    FROM inventories
                    WHERE profile = ? COLLATE NOCASE
                    ORDER BY generated_at DESC, id DESC
                    LIMIT ?;
                    """,
                    (configured_profile, limit),
                ).fetchall()
            else:
                total = 1
                rows = connection.execute(
                    """
                    SELECT id, profile, generated_at, stored_at, analyzer_version,
                           metadata_json, asset_count, relation_count, error_count,
                           length(CAST(metadata_json AS BLOB)) AS metadata_bytes
                    FROM inventories
                    WHERE profile = ? COLLATE NOCASE AND id = ?
                    LIMIT 1;
                    """,
                    (configured_profile, inventory_id),
                ).fetchall()
            receipts: list[dict[str, Any]] = []
            for row in rows:
                try:
                    receipts.append(self._inventory_receipt(row, configured_profile))
                except UnpackedAssetJobError:
                    if inventory_id is not None:
                        raise
            return receipts, total
        except sqlite3.Error as error:
            raise UnpackedAssetJobError("UEX Inventory query failed closed") from error
        finally:
            connection.close()

    def discover_uex_decisions(
        self, *, profile: str = "", inventory_limit: int = 20
    ) -> dict[str, Any]:
        """Enumerate only safe profile names and committed Inventory decisions."""

        if (
            isinstance(inventory_limit, bool)
            or not isinstance(inventory_limit, int)
            or not 1 <= inventory_limit <= _MAX_INVENTORIES
        ):
            raise UnpackedAssetJobError("inventory_limit is outside its fixed bound")
        base = {
            "schema": DECISION_DISCOVERY_SCHEMA,
            "schemaVersion": 1,
            "available": False,
            "status": "unavailable",
            "profiles": [],
            "coverage": {
                "configuredProfileCount": 0,
                "returnedProfileCount": 0,
                "profileEnumerationTruncated": False,
                "inventoryEnumerationTruncated": False,
            },
            "sourceContract": {
                "provider": "UEX",
                "inventorySchemaVersion": _SUPPORTED_UEX_INVENTORY_SCHEMA,
                "readOnlyCommittedRows": True,
                "cliInventoryListHasQualityMetadata": False,
                "hostReadsWhitelistedInventoryColumns": True,
            },
            "policy": {
                "profileNamesReturned": True,
                "profilePathsReturned": False,
                "aesValuesReturned": False,
                "arbitraryPathsAccepted": False,
                "partialInventoriesMarked": True,
            },
        }
        try:
            configured = self._configured_profiles()
        except UnpackedAssetJobError:
            base["reasonCode"] = "uex-profiles-unavailable"
            return base
        base["available"] = True
        base["coverage"]["configuredProfileCount"] = len(configured)
        if profile:
            try:
                selected = [self._select_profile(configured, profile)]
            except UnpackedAssetJobError:
                base["status"] = "not-found"
                base["reasonCode"] = "profile-not-configured"
                return base
        else:
            selected = list(configured[:_MAX_RETURNED_PROFILES])
            base["coverage"]["profileEnumerationTruncated"] = len(configured) > len(selected)

        profile_receipts: list[dict[str, Any]] = []
        unavailable_count = 0
        for candidate in selected:
            entry: dict[str, Any] = {
                "profile": candidate["profile"],
                "status": "unavailable",
                "inventoryStoreSchemaVersion": None,
                "inventoryCount": 0,
                "returnedInventoryCount": 0,
                "truncated": False,
                "inventories": [],
            }
            try:
                inventories, total = self._inventory_records(
                    candidate, limit=inventory_limit
                )
            except (UnpackedAssetJobError, sqlite3.Error):
                entry["reasonCode"] = "inventory-store-unavailable"
                unavailable_count += 1
            else:
                entry.update(
                    {
                        "status": "ready",
                        "inventoryStoreSchemaVersion": _SUPPORTED_UEX_INVENTORY_SCHEMA,
                        "inventoryCount": total,
                        "returnedInventoryCount": len(inventories),
                        "truncated": total > len(inventories),
                        "inventories": inventories,
                    }
                )
            profile_receipts.append(entry)
        base["profiles"] = profile_receipts
        base["coverage"]["returnedProfileCount"] = len(profile_receipts)
        base["coverage"]["inventoryEnumerationTruncated"] = any(
            receipt.get("truncated") is True for receipt in profile_receipts
        )
        if not profile_receipts or unavailable_count == len(profile_receipts):
            base["status"] = "unavailable"
        elif (
            unavailable_count
            or base["coverage"]["profileEnumerationTruncated"]
            or base["coverage"]["inventoryEnumerationTruncated"]
        ):
            base["status"] = "partial"
        else:
            base["status"] = "ready"
        return base

    def resolve_inventory_decision(self, profile: str, inventory_id: int) -> dict[str, Any]:
        if isinstance(inventory_id, bool) or not isinstance(inventory_id, int) or inventory_id <= 0:
            raise UnpackedAssetJobError("inventory_id must be a positive integer")
        selected = self._select_profile(self._configured_profiles(), profile)
        receipts, unused_total = self._inventory_records(
            selected, limit=1, inventory_id=inventory_id
        )
        if not receipts:
            raise UnpackedAssetJobError("requested UEX Inventory was not found")
        return receipts[0]

    def query_uex_inventory_assets(
        self,
        *,
        profile: str,
        inventory_id: int,
        kind: str = "",
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return bounded asset identities without provider detail JSON or local paths."""

        if isinstance(inventory_id, bool) or not isinstance(inventory_id, int) or inventory_id <= 0:
            raise UnpackedAssetJobError("inventory_id must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= _MAX_ASSET_QUERY_OFFSET:
            raise UnpackedAssetJobError("offset is outside its fixed bound")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_ASSET_QUERY_ROWS:
            raise UnpackedAssetJobError("limit is outside its fixed bound")
        if not isinstance(kind, str) or len(kind) > 64 or any(ord(character) < 32 for character in kind):
            raise UnpackedAssetJobError("kind is invalid")
        if (
            not isinstance(search, str)
            or len(search) > _MAX_ASSET_SEARCH_CHARS
            or any(ord(character) < 32 for character in search)
        ):
            raise UnpackedAssetJobError("search is invalid")
        selected = self._select_profile(self._configured_profiles(), profile)
        decision = self.resolve_inventory_decision(str(selected["profile"]), inventory_id)
        connection = self._open_inventory_database(selected)
        try:
            clauses = ["inventory_id = ?"]
            parameters: list[Any] = [inventory_id]
            if kind.strip():
                clauses.append("kind = ? COLLATE NOCASE")
                parameters.append(kind.strip())
            if search:
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("(path LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
                parameters.extend(("%" + escaped + "%", "%" + escaped + "%"))
            where = " AND ".join(clauses)
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM assets WHERE " + where + ";", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT path, name, class, kind, package_size, estimated_memory_size,
                       available, error, metrics_json
                FROM assets
                WHERE """
                + where
                + " ORDER BY path COLLATE NOCASE LIMIT ? OFFSET ?;",
                [*parameters, limit, offset],
            ).fetchall()
        except sqlite3.Error as error:
            raise UnpackedAssetJobError("UEX Inventory asset query failed closed") from error
        finally:
            connection.close()

        items: list[dict[str, Any]] = []
        invalid_count = 0
        for row in rows:
            try:
                inventory_path = str(row["path"] or "").strip().replace("\\", "/")
                export_path = _normalize_export_asset_path(inventory_path)
                if len(inventory_path) > 2048 or any(ord(character) < 32 for character in inventory_path):
                    raise UnpackedAssetJobError("inventory asset identity is invalid")
                package_size = row["package_size"]
                estimated = row["estimated_memory_size"]
                if (
                    isinstance(package_size, bool)
                    or not isinstance(package_size, int)
                    or package_size < 0
                    or (
                        estimated is not None
                        and (isinstance(estimated, bool) or not isinstance(estimated, int) or estimated < 0)
                    )
                ):
                    raise UnpackedAssetJobError("inventory asset sizes are invalid")
            except UnpackedAssetJobError:
                invalid_count += 1
                continue
            available = bool(row["available"] == 1)
            items.append(
                {
                    "inventoryPath": inventory_path,
                    "exportAssetPath": export_path,
                    "name": _bounded_text(row["name"], 256),
                    "assetClass": _bounded_text(row["class"], 128) or "Unknown",
                    "kind": _bounded_text(row["kind"], 128) or "Unknown",
                    "packageBytes": package_size,
                    "estimatedMemoryBytes": estimated,
                    "availability": "available" if available else "unavailable",
                    "hasProviderError": bool(row["error"] not in (None, "")),
                    "metrics": _safe_asset_metrics(row["metrics_json"]),
                }
            )
        return {
            "schema": INVENTORY_ASSET_QUERY_SCHEMA,
            "schemaVersion": 1,
            "status": "partial" if invalid_count else "ready",
            "profile": selected["profile"],
            "inventory": decision,
            "query": {
                "kind": kind.strip(),
                "search": search,
                "offset": offset,
                "limit": limit,
            },
            "total": total,
            "returned": len(items),
            "invalidOmitted": invalid_count,
            "truncated": offset + len(rows) < total,
            "items": items,
            "pathContract": {
                "exportField": "exportAssetPath",
                "leadingSlashRequired": False,
                "sameNameObjectSuffixRemoved": True,
                "packageExtensionOptional": True,
            },
            "policy": {
                "providerDetailJsonReturned": False,
                "profilePathsReturned": False,
                "aesValuesReturned": False,
            },
        }

    def _resolve_blender_inventory_selection(
        self,
        *,
        profile: str,
        inventory_id: int,
        expected_decision_sha256: str,
        assets: Sequence[str],
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """Resolve exact available SkeletalMesh rows from one immutable Inventory.

        The Inventory quality row and selected asset rows are read through the
        same read-only SQLite connection. Caller casing and object suffixes are
        replaced with the Inventory's canonical package identity before the
        effect is dispatched.
        """

        if (
            isinstance(inventory_id, bool)
            or not isinstance(inventory_id, int)
            or inventory_id <= 0
        ):
            raise UnpackedAssetJobError("inventory_id must be a positive integer")
        if (
            not isinstance(expected_decision_sha256, str)
            or not _SHA256_RE.fullmatch(expected_decision_sha256)
        ):
            raise UnpackedAssetJobError(
                "expected_inventory_decision_sha256 must be a lowercase SHA-256"
            )
        selected_profile = self._select_profile(self._configured_profiles(), profile)
        canonical_profile = str(selected_profile["profile"])
        connection = self._open_inventory_database(selected_profile)
        try:
            connection.execute("BEGIN;")
            inventory_row = connection.execute(
                """
                SELECT id, profile, generated_at, stored_at, analyzer_version,
                       metadata_json, asset_count, relation_count, error_count,
                       length(CAST(metadata_json AS BLOB)) AS metadata_bytes
                FROM inventories
                WHERE profile = ? COLLATE NOCASE AND id = ?
                LIMIT 1;
                """,
                (canonical_profile, inventory_id),
            ).fetchone()
            if inventory_row is None:
                raise UnpackedAssetJobError("requested UEX Inventory was not found")
            decision = self._inventory_receipt(inventory_row, canonical_profile)
            if decision["decisionSha256"] != expected_decision_sha256:
                raise UnpackedAssetJobError(
                    "UEX Inventory decision changed; refresh discovery before export"
                )

            variants: set[str] = set()
            for asset in assets:
                leaf = asset.rsplit("/", 1)[-1]
                variants.update(
                    {
                        asset,
                        asset + "." + leaf,
                        asset + ".uasset",
                        asset + ".umap",
                    }
                )
            ordered_variants = sorted(variants, key=str.casefold)
            placeholders = ",".join("?" for _ in ordered_variants)
            rows = connection.execute(
                """
                SELECT path, class, kind, available, error
                FROM assets
                WHERE inventory_id = ?
                  AND ltrim(replace(path, char(92), '/'), '/') COLLATE NOCASE IN (
                """
                + placeholders
                + ") ORDER BY path COLLATE NOCASE;",
                [inventory_id, *ordered_variants],
            ).fetchall()
        except sqlite3.Error as error:
            raise UnpackedAssetJobError(
                "UEX Inventory exact asset resolution failed closed"
            ) from error
        finally:
            connection.close()

        by_asset: dict[str, list[dict[str, Any]]] = {
            asset.casefold(): [] for asset in assets
        }
        for row in rows:
            inventory_path = str(row["path"] or "").strip().replace("\\", "/")
            try:
                export_path = _normalize_export_asset_path(inventory_path)
            except UnpackedAssetJobError:
                continue
            bucket = by_asset.get(export_path.casefold())
            if bucket is None:
                continue
            asset_class = _bounded_text(row["class"], 128)
            kind = _bounded_text(row["kind"], 128)
            available = row["available"]
            provider_error = row["error"]
            if kind.casefold() != "skeletalmesh":
                raise UnpackedAssetJobError(
                    "selected Inventory asset kind is not SkeletalMesh: %s" % export_path
                )
            if asset_class.casefold() not in {"skeletalmesh", "uskeletalmesh"}:
                raise UnpackedAssetJobError(
                    "selected Inventory asset class is not SkeletalMesh: %s" % export_path
                )
            if isinstance(available, bool) or not isinstance(available, int) or available != 1:
                raise UnpackedAssetJobError(
                    "selected Inventory SkeletalMesh is unavailable: %s" % export_path
                )
            if provider_error not in (None, ""):
                raise UnpackedAssetJobError(
                    "selected Inventory SkeletalMesh has a provider error: %s" % export_path
                )
            bucket.append(
                _inventory_asset_identity_receipt(
                    inventory_path=inventory_path,
                    export_asset_path=export_path,
                    asset_class=asset_class,
                    kind=kind,
                )
            )

        selection: list[dict[str, Any]] = []
        for asset in assets:
            matches = by_asset[asset.casefold()]
            if not matches:
                raise UnpackedAssetJobError(
                    "selected SkeletalMesh is not present in the bound Inventory: %s" % asset
                )
            if len(matches) != 1:
                raise UnpackedAssetJobError(
                    "selected SkeletalMesh has an ambiguous Inventory identity: %s" % asset
                )
            selection.append(matches[0])
        return canonical_profile, decision, selection

    def capabilities(self) -> dict[str, Any]:
        paths = self._provider_paths()
        uex = paths["uex"]
        profiles = paths["profiles"]
        blender = paths["blender"]
        wrapper = paths["wrapper"]
        python = paths["python"]
        addon = paths["ueformatAddon"]
        assembler = paths["blenderAssembler"]
        validator = paths["blenderValidator"]
        continuation_driver = paths["blenderContinuationDriver"]
        return {
            "schema": CAPABILITIES_SCHEMA,
            "schemaVersion": 1,
            "providers": {
                "uex": _provider_fingerprint(uex),
                "uexProfiles": _provider_fingerprint(profiles),
                "blender": _provider_fingerprint(blender),
                "uexBlenderWrapper": _provider_fingerprint(wrapper),
                "uexPython": _provider_fingerprint(python),
                "blenderAssembler": _provider_fingerprint(assembler),
                "blenderValidator": _provider_fingerprint(validator),
                "blenderContinuationDriver": _provider_fingerprint(
                    continuation_driver
                ),
                "ueformatAddon": {
                    "available": addon is not None,
                    "name": addon.name if addon is not None else "",
                    "sha256": (
                        ""
                        if addon is None
                        else _sha256_file(addon)
                        if addon.is_file()
                        else "directory"
                    ),
                },
            },
            "routes": {
                "uexDecisionDiscovery": {
                    "available": profiles is not None,
                    "readOnly": True,
                    "inventorySchemaVersion": _SUPPORTED_UEX_INVENTORY_SCHEMA,
                    "requires": ["profiles", "committed UEX inventory.db v2"],
                },
                "assetReport": {
                    "available": uex is not None and profiles is not None,
                    "requires": [
                        "UEX",
                        "profiles",
                        "Strong complete concrete Inventory",
                        "bounded project-specific v1 grouping rules",
                    ],
                },
                "blenderBundleOnly": {
                    "available": all(item is not None for item in (uex, profiles, wrapper, python)),
                    "inventoryDecisionRequired": True,
                    "exactAssetMembershipRequired": True,
                    "requires": [
                        "UEX",
                        "profiles",
                        "fixed wrapper",
                        "fixed Python runtime",
                        "exact available SkeletalMesh rows in one decision-hash-bound Inventory",
                    ],
                },
                "blenderAssembly": {
                    "available": all(item is not None for item in (uex, profiles, wrapper, python, blender)),
                    "ueModelPreferredAvailable": addon is not None,
                    "inventoryDecisionRequired": True,
                    "exactAssetMembershipRequired": True,
                    "requires": [
                        "UEX",
                        "profiles",
                        "fixed wrapper",
                        "fixed Python runtime",
                        "Blender",
                        "exact available SkeletalMesh rows in one decision-hash-bound Inventory",
                    ],
                },
                "blenderContinuation": {
                    "available": all(
                        item is not None
                        for item in (
                            uex,
                            profiles,
                            wrapper,
                            python,
                            blender,
                            assembler,
                            validator,
                            continuation_driver,
                        )
                    ),
                    "parentReceiptRequired": True,
                    "successfulBundlesReexported": False,
                    "hostResolvesParentArtifacts": True,
                    "sharedAssemblyValidated": True,
                    "requires": [
                        "terminal partial Blender parent receipt",
                        "parent request and result SHA-256",
                        "fixed export-only wrapper",
                        "fixed Blender assembler and validator",
                        "fixed Host continuation driver",
                    ],
                },
            },
            "managedRoot": str(self.root),
            "limits": {
                "maximumAssetsPerJob": _MAX_ASSETS,
                "maximumProfilesPerDiscovery": _MAX_RETURNED_PROFILES,
                "maximumInventoriesPerProfile": _MAX_INVENTORIES,
                "maximumInventoryAssetQueryRows": _MAX_ASSET_QUERY_ROWS,
            },
            "policy": {
                "callerExecutablePathsAccepted": False,
                "callerOutputPathsAccepted": False,
                "callerProfileDatabasePathsAccepted": False,
                "shellExecution": False,
                "aesValuesReturned": False,
                "profilePathsReturned": False,
                "idempotentOperationNonce": True,
                "continuationCallerArtifactPathsAccepted": False,
                "continuationRetainsValidatedParentBundles": True,
            },
            "reportSpecification": {
                "hostManagedCreationAvailable": True,
                "inlineManagedStagingAvailable": True,
                "legacyCatalogAssetCompatible": True,
                "canonicalRequestHashBound": True,
                "callerPathsAccepted": False,
                "requiredDecision": (
                    "Project-specific character and scene grouping rules are required; "
                    "the Host never supplies game-specific defaults."
                ),
                "missingRulesStatus": "yield",
                "decisionOwner": "child-agent-with-project-evidence-or-user",
            },
        }

    def _job_dir(self, operation_id: str, *, must_exist: bool) -> Path:
        if not isinstance(operation_id, str) or not _HEX32_RE.fullmatch(operation_id):
            raise UnpackedAssetJobError("operation_id must be 32 lowercase hexadecimal characters")
        root = self._ensure_root()
        jobs = root / "jobs"
        jobs.mkdir(exist_ok=True)
        if jobs.is_symlink() or not jobs.is_dir():
            raise UnpackedAssetJobError("unpacked-asset jobs directory is redirected")
        path = jobs / operation_id
        if must_exist and (path.is_symlink() or not path.is_dir()):
            raise UnpackedAssetJobError("unpacked-asset job does not exist")
        return path

    def _state_path(self, operation_id: str) -> Path:
        return self._job_dir(operation_id, must_exist=True) / "job.json"

    def _load(self, operation_id: str) -> dict[str, Any]:
        value = _read_json(self._state_path(operation_id), _MAX_STATE_BYTES)
        if (
            value.get("schema") != JOB_SCHEMA
            or value.get("schemaVersion") != JOB_VERSION
            or value.get("operationId") != operation_id
        ):
            raise UnpackedAssetJobError("unpacked-asset job state is incompatible")
        return value

    def _start(
        self,
        *,
        operation_id: str,
        kind: str,
        request: Mapping[str, Any],
        command: Sequence[str],
        staged_files: Mapping[str, bytes] | None = None,
    ) -> dict[str, Any]:
        staged_payloads: dict[str, bytes] = {}
        staged_records: list[dict[str, Any]] = []
        if staged_files:
            if len(staged_files) > 4:
                raise UnpackedAssetJobError("managed staged input count exceeds its fixed bound")
            for raw_relative, raw_payload in sorted(staged_files.items()):
                if not isinstance(raw_relative, str) or not isinstance(
                    raw_payload, (bytes, bytearray)
                ):
                    raise UnpackedAssetJobError("managed staged input is invalid")
                relative = Path(raw_relative)
                if (
                    relative.is_absolute()
                    or relative.parts != ("input", relative.name)
                    or relative.name in {"", ".", ".."}
                    or relative.as_posix() != raw_relative
                ):
                    raise UnpackedAssetJobError("managed staged input path is invalid")
                payload = bytes(raw_payload)
                maximum_payload = (
                    _MAX_STATE_BYTES
                    if raw_relative == "input/continuation-plan.json"
                    else _MAX_REPORT_SPEC_BYTES
                )
                if not 1 <= len(payload) <= maximum_payload:
                    raise UnpackedAssetJobError("managed staged input exceeds its fixed byte bound")
                staged_payloads[raw_relative] = payload
                staged_records.append(
                    {
                        "relativePath": raw_relative,
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
        if "stagedInputs" in request:
            raise UnpackedAssetJobError("request cannot override Host-managed staged inputs")
        request_with_staging = dict(request)
        if staged_records:
            request_with_staging["stagedInputs"] = staged_records
        request_value = json.loads(_canonical_bytes(request_with_staging).decode("utf-8"))
        request_sha256 = hashlib.sha256(_canonical_bytes(request_value)).hexdigest()

        def verify_staged_inputs(job_dir: Path) -> None:
            if not staged_records:
                return
            input_root = job_dir / "input"
            if _is_redirected(input_root) or not input_root.is_dir():
                raise UnpackedAssetJobError("managed staged input directory is redirected")
            expected_names = {Path(record["relativePath"]).name for record in staged_records}
            try:
                entries = list(input_root.iterdir())
            except OSError as error:
                raise UnpackedAssetJobError("managed staged inputs are unavailable") from error
            if {entry.name for entry in entries} != expected_names or any(
                _is_redirected(entry) or not entry.is_file() for entry in entries
            ):
                raise UnpackedAssetJobError("managed staged input set changed")
            by_name = {Path(record["relativePath"]).name: record for record in staged_records}
            for entry in entries:
                record = by_name[entry.name]
                if (
                    entry.stat().st_size != record["size"]
                    or _sha256_file(
                        entry,
                        _MAX_STATE_BYTES
                        if entry.name == "continuation-plan.json"
                        else _MAX_REPORT_SPEC_BYTES,
                    )
                    != record["sha256"]
                ):
                    raise UnpackedAssetJobError("managed staged input content changed")

        with self._guard:
            job_dir = self._job_dir(operation_id, must_exist=False)
            try:
                job_dir.mkdir()
                created = True
            except FileExistsError:
                created = False
            if not created:
                state = self._load(operation_id)
                if (
                    state.get("kind") != kind
                    or state.get("requestSha256") != request_sha256
                    or state.get("request") != request_value
                ):
                    raise UnpackedAssetJobError("operation_id is bound to another unpacked-asset request")
                if state.get("setupComplete") is False and state.get("status") == "failed":
                    return self._public_state(state, job_dir)
                verify_staged_inputs(job_dir)
                return self.status(operation_id)
            state = {
                "schema": JOB_SCHEMA,
                "schemaVersion": JOB_VERSION,
                "operationId": operation_id,
                "kind": kind,
                "requestSha256": request_sha256,
                "request": request_value,
                "status": "queued",
                "pid": None,
                "exitCode": None,
                "result": None,
                "error": "",
                "setupComplete": False,
                "createdUnixMs": int(time.time() * 1000),
                "updatedUnixMs": int(time.time() * 1000),
            }
            _atomic_json(job_dir / "job.json", state)
            stdout = None
            stderr = None
            try:
                target = job_dir / "target"
                target.mkdir()
                if staged_records:
                    input_root = job_dir / "input"
                    input_root.mkdir()
                    if _is_redirected(input_root) or not input_root.is_dir():
                        raise UnpackedAssetJobError(
                            "managed staged input directory is redirected"
                        )
                    for record in staged_records:
                        destination = input_root / Path(record["relativePath"]).name
                        with destination.open("xb") as handle:
                            handle.write(staged_payloads[record["relativePath"]])
                            handle.flush()
                            os.fsync(handle.fileno())
                    verify_staged_inputs(job_dir)
                stdout_path = job_dir / "stdout.log"
                stderr_path = job_dir / "stderr.log"
                stdout = stdout_path.open("xb")
                stderr = stderr_path.open("xb")
            except Exception as error:
                if stdout is not None:
                    stdout.close()
                if stderr is not None:
                    stderr.close()
                state["status"] = "failed"
                state["error"] = _redact("managed job setup failed: %s" % error)
                state["updatedUnixMs"] = int(time.time() * 1000)
                _atomic_json(job_dir / "job.json", state)
                return self._public_state(state, job_dir)
            state["setupComplete"] = True
            state["updatedUnixMs"] = int(time.time() * 1000)
            _atomic_json(job_dir / "job.json", state)
            try:
                creation_flags = (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0
                )
                process = subprocess.Popen(
                    list(command),
                    cwd=str(job_dir),
                    env=_safe_environment(self.environment, job_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    creationflags=creation_flags,
                )
            except Exception as error:
                stdout.close()
                stderr.close()
                state["status"] = "failed"
                state["error"] = _redact("provider could not start: %s" % error)
                state["updatedUnixMs"] = int(time.time() * 1000)
                _atomic_json(job_dir / "job.json", state)
                return self._public_state(state, job_dir)
            state["status"] = "running"
            state["pid"] = int(process.pid)
            state["updatedUnixMs"] = int(time.time() * 1000)
            _atomic_json(job_dir / "job.json", state)
            self._running[operation_id] = _RunningJob(process, stdout, stderr)
            return self._public_state(state, job_dir)

    def start_report(
        self,
        *,
        operation_id: str,
        profile: str,
        inventory_id: int,
        inventory_evidence_asset_id: str = "",
        inventory_evidence_sha256: str = "",
        spec_path: Path | None = None,
        spec_asset_id: str = "",
        catalog_sha256: str = "",
        allow_partial: bool = False,
        report_spec: Mapping[str, Any] | None = None,
        project_binding_sha256: str = "",
    ) -> dict[str, Any]:
        paths = self._provider_paths()
        uex = paths["uex"]
        profiles = paths["profiles"]
        if uex is None or profiles is None:
            raise UnpackedAssetJobError("UEX asset-report provider is not configured")
        if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
            raise UnpackedAssetJobError("profile is invalid")
        if isinstance(inventory_id, bool) or not isinstance(inventory_id, int) or inventory_id <= 0:
            raise UnpackedAssetJobError("a positive concrete Strong inventory_id is required")
        inventory_decision = self.resolve_inventory_decision(profile, inventory_id)
        if (
            inventory_decision.get("strong") is not True
            or inventory_decision.get("complete") is not True
            or inventory_decision.get("truncated") is not False
            or inventory_decision.get("reportEligible") is not True
        ):
            raise UnpackedAssetJobError(
                "the Host-owned UEX Inventory decision is not Strong and complete"
            )
        profile = str(inventory_decision["profile"])
        if not isinstance(allow_partial, bool):
            raise UnpackedAssetJobError("allow_partial must be a boolean")
        if allow_partial:
            raise UnpackedAssetJobError("partial UEX reporting requires separate explicit user authority")

        legacy_values = (
            inventory_evidence_asset_id,
            inventory_evidence_sha256,
            spec_asset_id,
            catalog_sha256,
        )
        legacy_requested = spec_path is not None or any(value != "" for value in legacy_values)
        if legacy_requested:
            if (
                not isinstance(inventory_evidence_asset_id, str)
                or not _ASSET_ID_RE.fullmatch(inventory_evidence_asset_id)
            ):
                raise UnpackedAssetJobError(
                    "verified supplemental Inventory evidence identity is required"
                )
            if (
                not isinstance(inventory_evidence_sha256, str)
                or not _SHA256_RE.fullmatch(inventory_evidence_sha256)
            ):
                raise UnpackedAssetJobError(
                    "verified supplemental Inventory evidence hash is required"
                )
            if not isinstance(spec_asset_id, str) or not _ASSET_ID_RE.fullmatch(spec_asset_id):
                raise UnpackedAssetJobError("catalog-bound report specification identity is required")
            if not isinstance(catalog_sha256, str) or not _SHA256_RE.fullmatch(catalog_sha256):
                raise UnpackedAssetJobError("catalog_sha256 must bind the selected report inputs")
        elif any(not isinstance(value, str) or value for value in legacy_values):
            raise UnpackedAssetJobError("legacy report bindings are invalid")

        if report_spec is not None and spec_path is not None:
            raise UnpackedAssetJobError("choose one report specification source")
        if report_spec is None and spec_path is None:
            raise UnpackedAssetJobError(
                "project-specific report_spec grouping rules are required; yield for a decision"
            )
        if spec_path is not None:
            raw_spec = _strict_json_object(Path(spec_path), _MAX_REPORT_SPEC_BYTES)
        else:
            if not isinstance(report_spec, Mapping):
                raise UnpackedAssetJobError("report_spec must be an object")
            raw_spec = report_spec
        normalized_spec = normalize_asset_report_spec(raw_spec)
        spec_payload = _canonical_bytes(normalized_spec)
        spec_sha256 = hashlib.sha256(spec_payload).hexdigest()
        spec_source = "catalog-managed-v1" if legacy_requested else "inline-managed-v1"
        if not isinstance(project_binding_sha256, str) or (
            project_binding_sha256
            and not _SHA256_RE.fullmatch(project_binding_sha256)
        ):
            raise UnpackedAssetJobError("project binding hash is invalid")

        job_dir = self._job_dir(operation_id, must_exist=False)
        output = job_dir / "target"
        staged_spec = job_dir / "input" / "report-spec.json"
        command = [
            str(uex),
            "--config",
            str(profiles),
            "asset-report",
            "--profile",
            profile,
            "--inventory",
            str(inventory_id),
            "--spec",
            str(staged_spec),
            "--out",
            str(output),
        ]
        return self._start(
            operation_id=operation_id,
            kind="uex-asset-report",
            request={
                "profile": profile,
                "inventoryId": inventory_id,
                "inventoryConfidence": "Strong",
                "inventoryDecisionSha256": inventory_decision["decisionSha256"],
                "inventoryEvidenceAssetId": inventory_evidence_asset_id,
                "inventoryEvidenceSha256": inventory_evidence_sha256,
                "inventoryEvidenceAuthority": "host-owned-uex-inventory",
                "allowPartial": False,
                "specAssetId": spec_asset_id,
                "specSha256": spec_sha256,
                "specBytes": len(spec_payload),
                "specSource": spec_source,
                "specName": normalized_spec["name"],
                "characterRuleCount": len(normalized_spec["characterPathRules"]),
                "sceneScopeRuleCount": len(normalized_spec["scene"]["scopeGlobs"]),
                "catalogSha256": catalog_sha256,
                "projectBindingSha256": project_binding_sha256,
                "providerSha256": _sha256_file(uex),
                "profilesSha256": _sha256_file(profiles, _MAX_MANIFEST_BYTES),
            },
            command=command,
            staged_files={"input/report-spec.json": spec_payload},
        )

    def start_blender_export(
        self,
        *,
        operation_id: str,
        profile: str,
        inventory_id: int,
        expected_inventory_decision_sha256: str,
        virtual_assets: Sequence[str],
        lod0_only: bool,
        export_morph_targets: bool,
        prefer_uemodel: bool,
        export_only: bool,
    ) -> dict[str, Any]:
        paths = self._provider_paths()
        uex, profiles, blender, wrapper, python, addon = (
            paths["uex"],
            paths["profiles"],
            paths["blender"],
            paths["wrapper"],
            paths["python"],
            paths["ueformatAddon"],
        )
        if uex is None or profiles is None or wrapper is None or python is None:
            raise UnpackedAssetJobError("UEX Blender bundle provider is not configured")
        if not export_only and blender is None:
            raise UnpackedAssetJobError("Blender assembly provider is not configured")
        if prefer_uemodel and not export_only and addon is None:
            raise UnpackedAssetJobError("preferred .uemodel route requires a configured UEFormat add-on")
        if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
            raise UnpackedAssetJobError("profile is invalid")
        if isinstance(virtual_assets, (str, bytes)):
            raise UnpackedAssetJobError("virtual_assets must be a bounded unique list")
        try:
            assets = tuple(_normalize_export_asset_path(item) for item in virtual_assets)
        except TypeError as error:
            raise UnpackedAssetJobError("virtual_assets must be a bounded unique list") from error
        if not 1 <= len(assets) <= _MAX_ASSETS or len(set(item.casefold() for item in assets)) != len(assets):
            raise UnpackedAssetJobError("virtual_assets must be a bounded unique list")
        profile, inventory_decision, inventory_assets = (
            self._resolve_blender_inventory_selection(
                profile=profile,
                inventory_id=inventory_id,
                expected_decision_sha256=expected_inventory_decision_sha256,
                assets=assets,
            )
        )
        assets = tuple(
            str(receipt["exportAssetPath"]) for receipt in inventory_assets
        )
        for value, name in (
            (lod0_only, "lod0_only"),
            (export_morph_targets, "export_morph_targets"),
            (prefer_uemodel, "prefer_uemodel"),
            (export_only, "export_only"),
        ):
            if not isinstance(value, bool):
                raise UnpackedAssetJobError("%s must be a boolean" % name)
        job_dir = self._job_dir(operation_id, must_exist=False)
        output_blend = job_dir / "target" / "UEX_Assembly.blend"
        command = [str(python), str(wrapper), "--uex", str(uex), "--config", str(profiles), "--profile", profile]
        for asset in assets:
            command.extend(("--asset", asset))
        command.extend(("--out", str(output_blend)))
        if blender is not None:
            command.extend(("--blender", str(blender)))
        if prefer_uemodel and addon is not None:
            command.extend(("--ueformat-addon", str(addon)))
        if lod0_only:
            command.append("--lod0-only")
        if not export_morph_targets:
            command.append("--no-morph-targets")
        if export_only:
            command.append("--export-only")
        return self._start(
            operation_id=operation_id,
            kind="uex-blender-export",
            request={
                "profile": profile,
                "inventoryId": inventory_id,
                "inventoryDecisionSha256": inventory_decision["decisionSha256"],
                "inventoryDecision": inventory_decision,
                "inventoryAssets": inventory_assets,
                "virtualAssets": list(assets),
                "lod0Only": lod0_only,
                "exportMorphTargets": export_morph_targets,
                "preferUeModel": prefer_uemodel,
                "exportOnly": export_only,
                "providerSha256": _sha256_file(uex),
                "profilesSha256": _sha256_file(profiles, _MAX_MANIFEST_BYTES),
                "wrapperSha256": _sha256_file(wrapper, _MAX_MANIFEST_BYTES),
                "pythonSha256": _sha256_file(python),
                "blenderSha256": _sha256_file(blender) if blender is not None else "",
            },
            command=command,
        )

    @staticmethod
    def _result_sha256(state: Mapping[str, Any]) -> str:
        result = state.get("result")
        if not isinstance(result, Mapping):
            raise UnpackedAssetJobError("parent Blender result receipt is unavailable")
        return hashlib.sha256(_canonical_bytes(result)).hexdigest()

    @staticmethod
    def _require_state_request_hash(state: Mapping[str, Any]) -> None:
        request = state.get("request")
        expected = state.get("requestSha256")
        if (
            not isinstance(request, Mapping)
            or not isinstance(expected, str)
            or not _SHA256_RE.fullmatch(expected)
            or hashlib.sha256(_canonical_bytes(request)).hexdigest() != expected
        ):
            raise UnpackedAssetJobError("managed Blender request hash changed")

    @staticmethod
    def _verify_continuation_plan_input(
        state: Mapping[str, Any], job_dir: Path
    ) -> None:
        request = state.get("request")
        staged = request.get("stagedInputs") if isinstance(request, Mapping) else None
        if not isinstance(staged, list) or len(staged) != 1:
            raise UnpackedAssetJobError("Blender continuation staged plan is unbound")
        record = staged[0]
        if (
            not isinstance(record, Mapping)
            or record.get("relativePath") != "input/continuation-plan.json"
            or isinstance(record.get("size"), bool)
            or not isinstance(record.get("size"), int)
            or not 1 <= record["size"] <= _MAX_STATE_BYTES
            or not isinstance(record.get("sha256"), str)
            or not _SHA256_RE.fullmatch(record["sha256"])
        ):
            raise UnpackedAssetJobError("Blender continuation staged plan receipt is invalid")
        input_root = job_dir / "input"
        plan = input_root / "continuation-plan.json"
        try:
            entries = list(input_root.iterdir())
        except OSError as error:
            raise UnpackedAssetJobError("Blender continuation staged plan is unavailable") from error
        if (
            _is_redirected(input_root)
            or len(entries) != 1
            or entries[0] != plan
            or _is_redirected(plan)
            or not plan.is_file()
            or plan.stat().st_size != record["size"]
            or _sha256_file(plan, _MAX_STATE_BYTES) != record["sha256"]
        ):
            raise UnpackedAssetJobError("Blender continuation staged plan changed")

    def _validate_retained_blender_item(
        self,
        *,
        parent_operation_id: str,
        parent_item: Mapping[str, Any],
        expected_asset: str,
        expected_identity_sha256: str,
    ) -> dict[str, Any]:
        source_operation_id = parent_item.get(
            "sourceOperationId", parent_operation_id
        )
        if (
            not isinstance(source_operation_id, str)
            or not _HEX32_RE.fullmatch(source_operation_id)
        ):
            raise UnpackedAssetJobError("retained bundle source operation is invalid")
        source_state = self._load(source_operation_id)
        self._require_state_request_hash(source_state)
        if source_state.get("status") not in {"completed", "partial"}:
            raise UnpackedAssetJobError("retained bundle source operation is not terminal")
        source_request_sha256 = parent_item.get(
            "sourceRequestSha256", source_state.get("requestSha256")
        )
        if source_request_sha256 != source_state.get("requestSha256"):
            raise UnpackedAssetJobError("retained bundle source request hash changed")
        source_request = source_state.get("request")
        if not isinstance(source_request, Mapping):
            raise UnpackedAssetJobError("retained bundle source request is unavailable")
        self._blender_inventory_binding(source_request)
        source_assets = source_request.get("virtualAssets")
        source_inventory_assets = source_request.get("inventoryAssets")
        if not isinstance(source_assets, list) or not isinstance(source_inventory_assets, list):
            raise UnpackedAssetJobError("retained bundle source selection is unavailable")
        matching = [
            index
            for index, asset in enumerate(source_assets)
            if asset == expected_asset
        ]
        if len(matching) != 1:
            raise UnpackedAssetJobError("retained bundle source asset is ambiguous")
        source_index = matching[0]
        source_identity = source_inventory_assets[source_index]
        if (
            not isinstance(source_identity, Mapping)
            or source_identity.get("identitySha256") != expected_identity_sha256
            or parent_item.get("inventoryAssetIdentitySha256")
            != expected_identity_sha256
        ):
            raise UnpackedAssetJobError("retained bundle Inventory identity changed")
        manifest_record = parent_item.get("bundleManifest")
        if not isinstance(manifest_record, Mapping):
            raise UnpackedAssetJobError("retained bundle manifest receipt is absent")
        source_root = self._job_dir(source_operation_id, must_exist=True) / "target"
        manifest_path = _managed_file(
            source_root,
            manifest_record.get("path"),
            "retained bundle manifest",
        )
        if (
            manifest_path.stat().st_size != manifest_record.get("size")
            or _sha256_file(manifest_path) != manifest_record.get("sha256")
        ):
            raise UnpackedAssetJobError("retained bundle manifest hash changed")
        verified = self._verify_blender_bundle_item(
            state=source_state,
            target=source_root,
            asset=expected_asset,
            index=source_index,
            bundle_record={
                "asset": expected_asset,
                "manifest": str(manifest_path),
                "directory": str(manifest_path.parent),
                "hasUeModel": bool(
                    isinstance(parent_item.get("geometry"), Mapping)
                    and parent_item["geometry"].get("hasUeModel") is True
                ),
            },
        )
        parent_artifacts = parent_item.get("artifacts")
        verified_artifact_bytes = _canonical_bytes(verified["artifacts"])
        artifact_set_sha256 = parent_item.get("artifactSetSha256")
        artifact_count = parent_item.get("artifactCount")
        exact_artifacts = (
            isinstance(parent_artifacts, list)
            and _canonical_bytes(parent_artifacts) == verified_artifact_bytes
        )
        hashed_artifacts = (
            parent_artifacts is None
            and artifact_count == len(verified["artifacts"])
            and artifact_set_sha256
            == hashlib.sha256(verified_artifact_bytes).hexdigest()
        )
        if not exact_artifacts and not hashed_artifacts:
            raise UnpackedAssetJobError("retained bundle artifact receipt changed")
        return {
            "asset": expected_asset,
            "sourceOperationId": source_operation_id,
            "sourceRequestSha256": source_request_sha256,
            "inventoryAssetIdentitySha256": expected_identity_sha256,
            "bundleManifest": dict(verified["bundleManifest"]),
            "artifacts": [dict(artifact) for artifact in verified["artifacts"]],
            "geometry": dict(verified["geometry"]),
            "materialCount": verified["materialCount"],
            "textureCount": verified["textureCount"],
            "sourceRoot": str(source_root.resolve(strict=True)),
        }

    def start_blender_continuation(
        self,
        *,
        operation_id: str,
        parent_operation_id: str,
        expected_parent_request_sha256: str,
        expected_parent_result_sha256: str,
    ) -> dict[str, Any]:
        """Retain a partial parent's valid bundles and retry only its open set."""

        if operation_id == parent_operation_id:
            raise UnpackedAssetJobError("continuation requires a fresh operation_id")
        if (
            not isinstance(expected_parent_request_sha256, str)
            or not _SHA256_RE.fullmatch(expected_parent_request_sha256)
            or not isinstance(expected_parent_result_sha256, str)
            or not _SHA256_RE.fullmatch(expected_parent_result_sha256)
        ):
            raise UnpackedAssetJobError("parent request/result SHA-256 binding is invalid")
        # Settle an owned process before freezing the parent terminal receipt.
        self.status(parent_operation_id)
        parent_state = self._load(parent_operation_id)
        self._require_state_request_hash(parent_state)
        parent_result = parent_state.get("result")
        parent_request = parent_state.get("request")
        if (
            parent_state.get("kind")
            not in {"uex-blender-export", BLENDER_CONTINUATION_KIND}
            or parent_state.get("status") != "partial"
            or parent_state.get("error") not in (None, "")
            or parent_state.get("requestSha256")
            != expected_parent_request_sha256
            or self._result_sha256(parent_state)
            != expected_parent_result_sha256
            or not isinstance(parent_result, Mapping)
            or not isinstance(parent_request, Mapping)
            or parent_result.get("complete") is not False
        ):
            raise UnpackedAssetJobError(
                "continuation parent is not the exact terminal partial receipt"
            )
        parent_binding = self._blender_inventory_binding(parent_request)
        parent_assets = parent_request.get("virtualAssets")
        parent_inventory_assets = parent_request.get("inventoryAssets")
        parent_items = parent_result.get("items")
        aggregate = parent_result.get("aggregate")
        if (
            not isinstance(parent_assets, list)
            or not 1 <= len(parent_assets) <= _MAX_ASSETS
            or not isinstance(parent_inventory_assets, list)
            or len(parent_inventory_assets) != len(parent_assets)
            or not isinstance(parent_items, list)
            or len(parent_items) != len(parent_assets)
            or not isinstance(aggregate, Mapping)
            or aggregate.get("assemblyAccepted") is not False
        ):
            raise UnpackedAssetJobError("continuation parent item receipt is inconsistent")

        retained: list[dict[str, Any]] = []
        retry_indices: list[int] = []
        for index, (asset, identity, raw_item) in enumerate(
            zip(parent_assets, parent_inventory_assets, parent_items)
        ):
            if not isinstance(identity, Mapping) or not isinstance(raw_item, Mapping):
                raise UnpackedAssetJobError("continuation parent item receipt is invalid")
            canonical_asset = _normalize_export_asset_path(asset)
            if (
                raw_item.get("asset") != canonical_asset
                or raw_item.get("index") != index
                or raw_item.get("inventoryId") != parent_binding["inventoryId"]
                or raw_item.get("inventoryDecisionSha256")
                != parent_binding["inventoryDecisionSha256"]
            ):
                raise UnpackedAssetJobError("continuation parent item order changed")
            identity_sha256 = identity.get("identitySha256")
            if not isinstance(identity_sha256, str) or not _SHA256_RE.fullmatch(
                identity_sha256
            ):
                raise UnpackedAssetJobError("continuation parent identity is invalid")
            if raw_item.get("status") == "completed" and raw_item.get("complete") is True:
                binding = self._validate_retained_blender_item(
                    parent_operation_id=parent_operation_id,
                    parent_item=raw_item,
                    expected_asset=canonical_asset,
                    expected_identity_sha256=identity_sha256,
                )
                binding["index"] = index
                retained.append(binding)
            elif raw_item.get("status") in {"failed", "open"} and raw_item.get("complete") is False:
                retry_indices.append(index)
            else:
                raise UnpackedAssetJobError("continuation parent item status is invalid")
        if not retained:
            raise UnpackedAssetJobError(
                "continuation parent has no validated successful bundle to retain"
            )

        profile, inventory_decision, inventory_assets = (
            self._resolve_blender_inventory_selection(
                profile=str(parent_binding["profile"]),
                inventory_id=int(parent_binding["inventoryId"]),
                expected_decision_sha256=str(
                    parent_binding["inventoryDecisionSha256"]
                ),
                assets=tuple(str(asset) for asset in parent_assets),
            )
        )
        if _canonical_bytes(inventory_assets) != _canonical_bytes(parent_inventory_assets):
            raise UnpackedAssetJobError(
                "continuation Inventory asset identities changed after the parent"
            )

        paths = self._provider_paths()
        required_names = (
            "uex",
            "profiles",
            "wrapper",
            "python",
            "blenderContinuationDriver",
        )
        if any(paths[name] is None for name in required_names):
            raise UnpackedAssetJobError("UEX Blender continuation provider is not configured")
        export_only = parent_request.get("exportOnly")
        if not isinstance(export_only, bool):
            raise UnpackedAssetJobError("continuation parent options are invalid")
        if not export_only and any(
            paths[name] is None
            for name in ("blender", "blenderAssembler", "blenderValidator")
        ):
            raise UnpackedAssetJobError(
                "Blender continuation assembly/validation provider is not configured"
            )
        if (
            parent_request.get("preferUeModel") is True
            and not export_only
            and paths["ueformatAddon"] is None
        ):
            raise UnpackedAssetJobError(
                "preferred .uemodel continuation requires a configured UEFormat add-on"
            )

        request = {
            "profile": profile,
            "inventoryId": parent_binding["inventoryId"],
            "inventoryDecisionSha256": inventory_decision["decisionSha256"],
            "inventoryDecision": inventory_decision,
            "inventoryAssets": inventory_assets,
            "virtualAssets": list(parent_assets),
            "lod0Only": bool(parent_request.get("lod0Only")),
            "exportMorphTargets": bool(parent_request.get("exportMorphTargets")),
            "preferUeModel": bool(parent_request.get("preferUeModel")),
            "exportOnly": export_only,
            "providerSha256": _sha256_file(paths["uex"]),
            "profilesSha256": _sha256_file(paths["profiles"], _MAX_MANIFEST_BYTES),
            "wrapperSha256": _sha256_file(paths["wrapper"], _MAX_MANIFEST_BYTES),
            "pythonSha256": _sha256_file(paths["python"]),
            "blenderSha256": (
                _sha256_file(paths["blender"])
                if paths["blender"] is not None
                else ""
            ),
            "assemblerSha256": (
                _sha256_file(paths["blenderAssembler"], _MAX_MANIFEST_BYTES)
                if paths["blenderAssembler"] is not None
                else ""
            ),
            "validatorSha256": (
                _sha256_file(paths["blenderValidator"], _MAX_MANIFEST_BYTES)
                if paths["blenderValidator"] is not None
                else ""
            ),
            "continuation": {
                "parentOperationId": parent_operation_id,
                "parentRequestSha256": expected_parent_request_sha256,
                "parentResultSha256": expected_parent_result_sha256,
                "retainedItems": [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"sourceRoot", "artifacts"}
                    }
                    | {
                        "artifactCount": len(item["artifacts"]),
                        "artifactSetSha256": hashlib.sha256(
                            _canonical_bytes(item["artifacts"])
                        ).hexdigest(),
                    }
                    for item in retained
                ],
                "retryIndices": retry_indices,
                "retryAssets": [parent_assets[index] for index in retry_indices],
            },
        }
        job_dir = self._job_dir(operation_id, must_exist=False)
        managed_root = self._ensure_root()
        retained_by_index = {int(item["index"]): item for item in retained}
        sources: list[dict[str, Any]] = []
        retry_position = {index: value for value, index in enumerate(retry_indices)}
        for index, asset in enumerate(parent_assets):
            if index in retained_by_index:
                item = retained_by_index[index]
                sources.append(
                    {
                        "origin": "retained",
                        "asset": asset,
                        "sourceOperationId": item["sourceOperationId"],
                        "sourceRoot": item["sourceRoot"],
                        "bundleManifest": item["bundleManifest"],
                        "artifacts": item["artifacts"],
                    }
                )
            else:
                sources.append(
                    {
                        "origin": "retry",
                        "asset": asset,
                        "retryIndex": retry_position[index],
                    }
                )

        def provider_plan(name: str) -> dict[str, Any] | None:
            path = paths[name]
            if path is None:
                return None
            return {"path": str(path), "sha256": _sha256_file(path)}

        plan = {
            "schema": BLENDER_CONTINUATION_PLAN_SCHEMA,
            "operationId": operation_id,
            "managedRoot": str(managed_root),
            "profile": profile,
            "sourceAssets": list(parent_assets),
            "retryAssets": [parent_assets[index] for index in retry_indices],
            "sources": sources,
            "options": {
                "lod0Only": request["lod0Only"],
                "exportMorphTargets": request["exportMorphTargets"],
                "preferUeModel": request["preferUeModel"],
                "exportOnly": export_only,
            },
            "parent": {
                "operationId": parent_operation_id,
                "requestSha256": expected_parent_request_sha256,
                "resultSha256": expected_parent_result_sha256,
            },
            "providers": {
                "uex": provider_plan("uex"),
                "profiles": provider_plan("profiles"),
                "wrapper": provider_plan("wrapper"),
                "python": provider_plan("python"),
                "blender": provider_plan("blender"),
                "assembler": provider_plan("blenderAssembler"),
                "validator": provider_plan("blenderValidator"),
                "driver": {
                    "path": str(paths["blenderContinuationDriver"]),
                    "sha256": _sha256_file(
                        paths["blenderContinuationDriver"], _MAX_MANIFEST_BYTES
                    ),
                },
            },
            "ueformatAddon": (
                str(paths["ueformatAddon"])
                if paths["ueformatAddon"] is not None
                else ""
            ),
        }
        plan_payload = _canonical_bytes(plan)
        if len(plan_payload) > _MAX_STATE_BYTES:
            raise UnpackedAssetJobError("Blender continuation plan exceeds its fixed bound")
        driver = paths["blenderContinuationDriver"]
        request["hostDriverSha256"] = _sha256_file(driver, _MAX_MANIFEST_BYTES)
        return self._start(
            operation_id=operation_id,
            kind=BLENDER_CONTINUATION_KIND,
            request=request,
            command=[
                str(paths["python"]),
                str(driver),
                "--run-blender-continuation",
                str(job_dir / "input" / "continuation-plan.json"),
            ],
            staged_files={"input/continuation-plan.json": plan_payload},
        )

    @staticmethod
    def _artifact(path: Path, root: Path) -> dict[str, Any]:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not path.is_file() or not _within(root, resolved):
            raise UnpackedAssetJobError("provider artifact escaped its managed output")
        return {
            "path": resolved.relative_to(root).as_posix(),
            "size": int(path.stat().st_size),
            "sha256": _sha256_file(path),
        }

    @staticmethod
    def _blender_inventory_binding(request: Mapping[str, Any]) -> dict[str, Any]:
        inventory_id = request.get("inventoryId")
        decision_sha256 = request.get("inventoryDecisionSha256")
        profile = request.get("profile")
        assets = request.get("virtualAssets")
        inventory_assets = request.get("inventoryAssets")
        decision = request.get("inventoryDecision")
        decision_projection = dict(decision) if isinstance(decision, Mapping) else {}
        decision_projection.pop("decisionSha256", None)
        if (
            isinstance(inventory_id, bool)
            or not isinstance(inventory_id, int)
            or inventory_id <= 0
            or not isinstance(decision_sha256, str)
            or not _SHA256_RE.fullmatch(decision_sha256)
            or not isinstance(profile, str)
            or not _PROFILE_RE.fullmatch(profile)
            or not isinstance(assets, list)
            or not isinstance(inventory_assets, list)
            or len(inventory_assets) != len(assets)
            or not isinstance(decision, Mapping)
            or decision.get("inventoryId") != inventory_id
            or decision.get("profile") != profile
            or decision.get("decisionSha256") != decision_sha256
            or decision.get("committed") is not True
            or hashlib.sha256(_canonical_bytes(decision_projection)).hexdigest()
            != decision_sha256
        ):
            raise UnpackedAssetJobError(
                "UEX Blender request omitted its Inventory decision binding"
            )

        identity_hashes: list[str] = []
        for index, (asset, raw_receipt) in enumerate(zip(assets, inventory_assets)):
            if not isinstance(raw_receipt, Mapping):
                raise UnpackedAssetJobError(
                    "UEX Blender Inventory asset receipt is invalid"
                )
            try:
                canonical_asset = _normalize_export_asset_path(asset)
                receipt_asset = _normalize_export_asset_path(
                    raw_receipt.get("exportAssetPath")
                )
                inventory_asset = _normalize_export_asset_path(
                    raw_receipt.get("inventoryPath")
                )
            except UnpackedAssetJobError as error:
                raise UnpackedAssetJobError(
                    "UEX Blender Inventory asset identity is invalid"
                ) from error
            asset_class = raw_receipt.get("assetClass")
            kind = raw_receipt.get("kind")
            identity_sha256 = raw_receipt.get("identitySha256")
            expected_receipt = _inventory_asset_identity_receipt(
                inventory_path=str(raw_receipt.get("inventoryPath")),
                export_asset_path=receipt_asset,
                asset_class=str(asset_class),
                kind=str(kind),
            )
            if (
                canonical_asset != receipt_asset
                or inventory_asset != receipt_asset
                or receipt_asset != asset
                or raw_receipt.get("available") is not True
                or raw_receipt.get("hasProviderError") is not False
                or not isinstance(kind, str)
                or kind.casefold() != "skeletalmesh"
                or not isinstance(asset_class, str)
                or asset_class.casefold() not in {"skeletalmesh", "uskeletalmesh"}
                or identity_sha256 != expected_receipt["identitySha256"]
            ):
                raise UnpackedAssetJobError(
                    "UEX Blender Inventory asset receipt changed at index %d" % index
                )
            identity_hashes.append(str(identity_sha256))
        if len(set(identity_hashes)) != len(identity_hashes):
            raise UnpackedAssetJobError(
                "UEX Blender Inventory asset receipts are not unique"
            )
        return {
            "profile": profile,
            "inventoryId": inventory_id,
            "inventoryDecisionSha256": decision_sha256,
            "inventoryAssetIdentitySha256": identity_hashes,
        }

    @classmethod
    def _blender_assembly_receipt(
        cls,
        request: Mapping[str, Any],
        *,
        accepted: bool,
        assembly_manifest: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        binding = cls._blender_inventory_binding(request)
        return {
            "schema": BLENDER_ASSEMBLY_RECEIPT_SCHEMA,
            "accepted": accepted,
            **binding,
            "sourceAssets": list(request["virtualAssets"]),
            "assemblyManifest": (
                dict(assembly_manifest) if assembly_manifest is not None else None
            ),
        }

    def _verify_report(self, state: Mapping[str, Any], job_dir: Path) -> dict[str, Any]:
        target = job_dir / "target"
        expected = (
            "asset-report.html",
            "asset-report.json",
            "asset-summary.csv",
            "asset-report-manifest.json",
        )
        paths = [target / name for name in expected]
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise UnpackedAssetJobError("UEX report omitted one or more portable artifacts")
        manifest = _read_json(paths[-1])
        report = _read_json(paths[1])
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("UEX report job request is unavailable")
        inventory = int(request["inventoryId"])
        profile = str(request["profile"])
        if (
            manifest.get("schemaVersion") != 1
            or manifest.get("kind") != "uex-asset-distribution-report"
            or manifest.get("reportSchemaVersion") != 1
            or manifest.get("inventoryId") != inventory
            or manifest.get("profile") != profile
        ):
            raise UnpackedAssetJobError("UEX report completion manifest changed its contract binding")
        if (
            report.get("schemaVersion") != 1
            or report.get("inventoryId") != inventory
            or report.get("profile") != profile
        ):
            raise UnpackedAssetJobError("UEX report JSON changed its Inventory or profile binding")

        html_size = paths[0].stat().st_size
        if not 1 <= html_size <= _MAX_MANIFEST_BYTES:
            raise UnpackedAssetJobError("UEX report HTML is outside its byte bound")
        try:
            html = paths[0].read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise UnpackedAssetJobError("UEX report HTML is not UTF-8") from error
        inventory_match = re.search(r'"inventoryId"\s*:\s*(-?[0-9]+)', html)
        profile_match = re.search(r'"profile"\s*:\s*("(?:\\.|[^"\\])*")', html)
        if (
            inventory_match is None
            or int(inventory_match.group(1)) != inventory
            or profile_match is None
            or json.loads(profile_match.group(1)) != profile
        ):
            raise UnpackedAssetJobError("UEX report HTML changed its Inventory or profile binding")
        if re.search(r"(?:src|href)\s*=\s*['\"]https?://", html, re.IGNORECASE):
            raise UnpackedAssetJobError("UEX report HTML is not self-contained")

        actual = {
            artifact["path"]: artifact
            for artifact in (self._artifact(path, target) for path in paths[:-1])
        }
        listed = manifest.get("artifacts")
        if not isinstance(listed, list) or len(listed) != len(actual):
            raise UnpackedAssetJobError("UEX report manifest has an incomplete artifact set")
        seen: set[str] = set()
        for index, record in enumerate(listed):
            if not isinstance(record, Mapping):
                raise UnpackedAssetJobError("UEX report manifest artifact %d is invalid" % index)
            name = record.get("fileName")
            if not isinstance(name, str) or name not in actual or name in seen:
                raise UnpackedAssetJobError("UEX report manifest artifact names are not exact and unique")
            seen.add(name)
            byte_size = record.get("bytes")
            if isinstance(byte_size, bool) or not isinstance(byte_size, int):
                raise UnpackedAssetJobError("UEX report manifest artifact size is invalid")
            if byte_size != actual[name]["size"] or record.get("sha256") != actual[name]["sha256"]:
                raise UnpackedAssetJobError("UEX report manifest artifact size/hash changed")
        if seen != set(actual):
            raise UnpackedAssetJobError("UEX report manifest omitted a portable artifact")
        completion = self._artifact(paths[-1], target)
        artifacts = [actual[name] for name in expected[:-1]] + [completion]
        return {
            "inventoryId": inventory,
            "inventoryConfidence": "Strong",
            "complete": True,
            "artifacts": artifacts,
            "completionManifest": completion,
        }

    @staticmethod
    def _bundle_directory_name(asset: str, index: int) -> str:
        leaf = asset.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        leaf = leaf.split(".", 1)[0]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", leaf).strip("._-")
        return "%02d_%s" % (index + 1, safe or "SkeletalMesh")

    def _verify_blender_bundle_item(
        self,
        *,
        state: Mapping[str, Any],
        target: Path,
        asset: str,
        index: int,
        bundle_record: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("UEX Blender job request is unavailable")
        canonical_asset = _normalize_export_asset_path(asset)
        if bundle_record is None:
            relative_manifest = (
                Path("UEX_Assembly_uex_export")
                / "bundles"
                / self._bundle_directory_name(canonical_asset, index)
                / "manifest.json"
            )
            manifest_path = _managed_file(
                target, relative_manifest.as_posix(), "bundle manifest"
            )
        else:
            try:
                record_asset = _normalize_export_asset_path(bundle_record.get("asset"))
            except UnpackedAssetJobError as error:
                raise UnpackedAssetJobError("UEX Blender bundle record asset is invalid") from error
            if record_asset != canonical_asset:
                raise UnpackedAssetJobError("UEX Blender bundle record changed asset identity")
            manifest_path = _managed_file(
                target, str(bundle_record.get("manifest", "") or ""), "bundle manifest"
            )
            directory_value = str(bundle_record.get("directory", "") or "")
            try:
                directory = Path(directory_value).resolve(strict=True)
            except OSError as error:
                raise UnpackedAssetJobError(
                    "UEX Blender bundle directory/manifest binding changed"
                ) from error
            if not directory_value or directory != manifest_path.parent:
                raise UnpackedAssetJobError(
                    "UEX Blender bundle directory/manifest binding changed"
                )

        manifest = _read_json(manifest_path)
        if manifest.get("schema") != "uex.blender-bundle.v1":
            raise UnpackedAssetJobError("UEX Blender bundle schema is unsupported")
        try:
            source_asset = _normalize_export_asset_path(manifest.get("sourceAsset"))
        except UnpackedAssetJobError as error:
            raise UnpackedAssetJobError("UEX Blender bundle sourceAsset is invalid") from error
        if source_asset != canonical_asset:
            raise UnpackedAssetJobError("UEX Blender bundle changed sourceAsset identity")
        if manifest.get("assetType") != "USkeletalMesh":
            raise UnpackedAssetJobError("UEX Blender bundle is not a SkeletalMesh")
        if not isinstance(manifest.get("skeleton"), str) or not manifest.get("skeleton"):
            raise UnpackedAssetJobError("UEX Blender bundle omitted its Skeleton identity")
        bone_count = manifest.get("boneCount")
        if isinstance(bone_count, bool) or not isinstance(bone_count, int) or bone_count <= 0:
            raise UnpackedAssetJobError("UEX Blender bundle has no verified bones")

        artifacts: list[dict[str, Any]] = []
        artifact_paths: set[str] = set()

        def remember(path: Path) -> dict[str, Any]:
            record = self._artifact(path, target)
            if record["path"] not in artifact_paths:
                artifact_paths.add(record["path"])
                artifacts.append(record)
            return record

        manifest_record = remember(manifest_path)
        bundle_root = manifest_path.parent
        importer = _bundle_relative_file(
            bundle_root, "import_to_blender.py", "bundle importer"
        )
        remember(importer)
        meshes = manifest.get("meshes")
        if not isinstance(meshes, list) or not meshes:
            raise UnpackedAssetJobError("UEX Blender bundle omitted mesh geometry")
        lod_indices: set[int] = set()
        for mesh_index, mesh in enumerate(meshes):
            if not isinstance(mesh, Mapping):
                raise UnpackedAssetJobError("UEX Blender mesh record is invalid")
            lod_index = mesh.get("lodIndex")
            if (
                isinstance(lod_index, bool)
                or not isinstance(lod_index, int)
                or lod_index < 0
                or lod_index in lod_indices
            ):
                raise UnpackedAssetJobError("UEX Blender mesh LOD identity is invalid")
            lod_indices.add(lod_index)
            mesh_path = _bundle_relative_file(
                bundle_root, mesh.get("relativePath"), "bundle mesh %d" % mesh_index
            )
            mesh_bytes = mesh.get("bytes")
            if (
                isinstance(mesh_bytes, bool)
                or not isinstance(mesh_bytes, int)
                or mesh_bytes != mesh_path.stat().st_size
            ):
                raise UnpackedAssetJobError("UEX Blender mesh byte count changed")
            remember(mesh_path)
        if 0 not in lod_indices:
            raise UnpackedAssetJobError("UEX Blender bundle omitted LOD0 geometry")
        if request["lod0Only"] and lod_indices != {0}:
            raise UnpackedAssetJobError("LOD0-only UEX request exported another LOD")

        ue_model_value = manifest.get("ueModel")
        has_ue_model = isinstance(ue_model_value, str) and bool(ue_model_value)
        if has_ue_model:
            remember(_bundle_relative_file(bundle_root, ue_model_value, "bundle .uemodel"))
        if bundle_record is not None and bool(bundle_record.get("hasUeModel")) != has_ue_model:
            raise UnpackedAssetJobError("UEX Blender bundle changed .uemodel availability")
        if request["preferUeModel"] and not has_ue_model:
            raise UnpackedAssetJobError("preferred .uemodel geometry is unavailable")

        materials = manifest.get("materials")
        textures = manifest.get("textures")
        if not isinstance(materials, list) or not isinstance(textures, list):
            raise UnpackedAssetJobError("UEX Blender bundle omitted material/texture metadata")
        for material_index, material in enumerate(materials):
            if not isinstance(material, Mapping):
                raise UnpackedAssetJobError("UEX Blender material record is invalid")
            remember(
                _bundle_relative_file(
                    bundle_root,
                    material.get("materialJson"),
                    "bundle material %d" % material_index,
                )
            )
            raw_material = material.get("rawMaterialJson")
            if raw_material not in (None, ""):
                remember(
                    _bundle_relative_file(
                        bundle_root,
                        raw_material,
                        "bundle raw material %d" % material_index,
                    )
                )
            base_color = material.get("baseColor")
            if isinstance(base_color, Mapping) and base_color.get("relativePath"):
                remember(
                    _bundle_relative_file(
                        bundle_root,
                        base_color.get("relativePath"),
                        "bundle base-color texture",
                    )
                )
            for parameter_key in ("directTextureParameters", "textureParameters"):
                parameters = material.get(parameter_key, [])
                if not isinstance(parameters, list):
                    raise UnpackedAssetJobError(
                        "UEX Blender material texture parameters are invalid"
                    )
                for parameter_index, parameter in enumerate(parameters):
                    if not isinstance(parameter, Mapping):
                        raise UnpackedAssetJobError(
                            "UEX Blender material texture parameter is invalid"
                        )
                    relative_parameter = parameter.get("relativePath")
                    if relative_parameter not in (None, ""):
                        remember(
                            _bundle_relative_file(
                                bundle_root,
                                relative_parameter,
                                "bundle material texture parameter %d" % parameter_index,
                            )
                        )
        for texture_index, texture in enumerate(textures):
            if not isinstance(texture, Mapping):
                raise UnpackedAssetJobError("UEX Blender texture record is invalid")
            if texture.get("error") not in (None, ""):
                raise UnpackedAssetJobError(
                    "UEX Blender bundle contains a failed texture export"
                )
            remember(
                _bundle_relative_file(
                    bundle_root,
                    texture.get("relativePath"),
                    "bundle texture %d" % texture_index,
                )
            )
        return {
            "asset": canonical_asset,
            "index": index,
            "inventoryId": request["inventoryId"],
            "inventoryDecisionSha256": request["inventoryDecisionSha256"],
            "inventoryAssetIdentitySha256": request["inventoryAssets"][index][
                "identitySha256"
            ],
            "status": "completed",
            "complete": True,
            "bundleManifest": manifest_record,
            "artifacts": artifacts,
            "geometry": {
                "lodIndices": sorted(lod_indices),
                "hasUeModel": has_ue_model,
                "boneCount": bone_count,
            },
            "materialCount": len(materials),
            "textureCount": len(textures),
        }

    def _collect_blender_bundle_items(
        self,
        state: Mapping[str, Any],
        job_dir: Path,
        bundle_records: Sequence[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        request = state.get("request")
        if not isinstance(request, Mapping) or not isinstance(request.get("virtualAssets"), list):
            raise UnpackedAssetJobError("UEX Blender job request is unavailable")
        binding = self._blender_inventory_binding(request)
        target = job_dir / "target"
        items: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        artifact_paths: set[str] = set()
        for index, asset in enumerate(request["virtualAssets"]):
            record = None
            if bundle_records is not None and index < len(bundle_records):
                candidate = bundle_records[index]
                record = candidate if isinstance(candidate, Mapping) else None
            try:
                item = self._verify_blender_bundle_item(
                    state=state,
                    target=target,
                    asset=str(asset),
                    index=index,
                    bundle_record=record,
                )
            except Exception as error:
                item = {
                    "asset": _normalize_export_asset_path(asset),
                    "index": index,
                    "inventoryId": binding["inventoryId"],
                    "inventoryDecisionSha256": binding[
                        "inventoryDecisionSha256"
                    ],
                    "inventoryAssetIdentitySha256": binding[
                        "inventoryAssetIdentitySha256"
                    ][index],
                    "status": "failed",
                    "complete": False,
                    "reason": _redact(str(error))[:1000],
                    "artifacts": [],
                }
            else:
                for artifact in item["artifacts"]:
                    if artifact["path"] not in artifact_paths:
                        artifact_paths.add(artifact["path"])
                        artifacts.append(artifact)
            items.append(item)
        return items, artifacts

    def _continuation_binding(self, request: Mapping[str, Any]) -> dict[str, Any]:
        base = self._blender_inventory_binding(request)
        continuation = request.get("continuation")
        if not isinstance(continuation, Mapping):
            raise UnpackedAssetJobError("Blender continuation binding is unavailable")
        parent_operation_id = continuation.get("parentOperationId")
        parent_request_sha256 = continuation.get("parentRequestSha256")
        parent_result_sha256 = continuation.get("parentResultSha256")
        retained = continuation.get("retainedItems")
        retry_indices = continuation.get("retryIndices")
        retry_assets = continuation.get("retryAssets")
        assets = request.get("virtualAssets")
        if (
            not isinstance(parent_operation_id, str)
            or not _HEX32_RE.fullmatch(parent_operation_id)
            or not isinstance(parent_request_sha256, str)
            or not _SHA256_RE.fullmatch(parent_request_sha256)
            or not isinstance(parent_result_sha256, str)
            or not _SHA256_RE.fullmatch(parent_result_sha256)
            or not isinstance(retained, list)
            or not retained
            or not isinstance(retry_indices, list)
            or not isinstance(retry_assets, list)
            or not isinstance(assets, list)
            or len(retry_indices) != len(retry_assets)
        ):
            raise UnpackedAssetJobError("Blender continuation binding is invalid")
        retained_indices: set[int] = set()
        for raw_item in retained:
            if not isinstance(raw_item, Mapping):
                raise UnpackedAssetJobError("Blender retained item binding is invalid")
            index = raw_item.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(assets)
                or index in retained_indices
                or raw_item.get("asset") != assets[index]
            ):
                raise UnpackedAssetJobError("Blender retained item order is invalid")
            retained_indices.add(index)
        expected_retry = [index for index in range(len(assets)) if index not in retained_indices]
        if retry_indices != expected_retry or retry_assets != [assets[index] for index in expected_retry]:
            raise UnpackedAssetJobError("Blender continuation retry set changed")
        return {
            **base,
            "parentOperationId": parent_operation_id,
            "parentRequestSha256": parent_request_sha256,
            "parentResultSha256": parent_result_sha256,
            "retainedItems": retained,
            "retryIndices": retry_indices,
            "retryAssets": retry_assets,
        }

    def _collect_blender_continuation_items(
        self, state: Mapping[str, Any], job_dir: Path
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path | None]]:
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("Blender continuation request is unavailable")
        binding = self._continuation_binding(request)
        assets = request["virtualAssets"]
        identities = request["inventoryAssets"]
        retained_by_index = {
            int(item["index"]): item for item in binding["retainedItems"]
        }
        retry_position = {
            int(index): position
            for position, index in enumerate(binding["retryIndices"])
        }
        target = job_dir / "target"
        items: list[dict[str, Any]] = []
        current_artifacts: list[dict[str, Any]] = []
        current_paths: set[str] = set()
        manifest_paths: list[Path | None] = []
        for index, asset in enumerate(assets):
            identity_sha256 = identities[index]["identitySha256"]
            if index in retained_by_index:
                raw_item = retained_by_index[index]
                try:
                    retained = self._validate_retained_blender_item(
                        parent_operation_id=binding["parentOperationId"],
                        parent_item=raw_item,
                        expected_asset=str(asset),
                        expected_identity_sha256=str(identity_sha256),
                    )
                    source_root = self._job_dir(
                        retained["sourceOperationId"], must_exist=True
                    ) / "target"
                    manifest_path = _managed_file(
                        source_root,
                        retained["bundleManifest"]["path"],
                        "retained bundle manifest",
                    )
                    item = {
                        "asset": asset,
                        "index": index,
                        "inventoryId": binding["inventoryId"],
                        "inventoryDecisionSha256": binding[
                            "inventoryDecisionSha256"
                        ],
                        "inventoryAssetIdentitySha256": identity_sha256,
                        "status": "completed",
                        "complete": True,
                        "retained": True,
                        "sourceOperationId": retained["sourceOperationId"],
                        "sourceRequestSha256": retained["sourceRequestSha256"],
                        "bundleManifest": retained["bundleManifest"],
                        "artifacts": retained["artifacts"],
                        "geometry": retained["geometry"],
                        "materialCount": retained["materialCount"],
                        "textureCount": retained["textureCount"],
                    }
                except Exception as error:
                    manifest_path = None
                    item = {
                        "asset": asset,
                        "index": index,
                        "inventoryId": binding["inventoryId"],
                        "inventoryDecisionSha256": binding[
                            "inventoryDecisionSha256"
                        ],
                        "inventoryAssetIdentitySha256": identity_sha256,
                        "status": "failed",
                        "complete": False,
                        "retained": True,
                        "reason": _redact(str(error))[:1000],
                        "artifacts": [],
                    }
            else:
                position = retry_position[index]
                expected_manifest = (
                    target
                    / "retry"
                    / "Retry_Assembly_uex_export"
                    / "bundles"
                    / self._bundle_directory_name(str(asset), position)
                    / "manifest.json"
                )
                try:
                    manifest_path = _managed_file(
                        target, str(expected_manifest), "retried bundle manifest"
                    )
                    item = self._verify_blender_bundle_item(
                        state=state,
                        target=target,
                        asset=str(asset),
                        index=index,
                        bundle_record={
                            "asset": asset,
                            "manifest": str(manifest_path),
                            "directory": str(manifest_path.parent),
                            "hasUeModel": bool(
                                _read_json(manifest_path).get("ueModel")
                            ),
                        },
                    )
                    item["retained"] = False
                    item["sourceOperationId"] = state["operationId"]
                    item["sourceRequestSha256"] = state["requestSha256"]
                    for artifact in item["artifacts"]:
                        if artifact["path"] not in current_paths:
                            current_paths.add(artifact["path"])
                            current_artifacts.append(artifact)
                except Exception as error:
                    manifest_path = None
                    item = {
                        "asset": asset,
                        "index": index,
                        "inventoryId": binding["inventoryId"],
                        "inventoryDecisionSha256": binding[
                            "inventoryDecisionSha256"
                        ],
                        "inventoryAssetIdentitySha256": identity_sha256,
                        "status": "failed",
                        "complete": False,
                        "retained": False,
                        "reason": _redact(str(error))[:1000],
                        "artifacts": [],
                    }
            items.append(item)
            manifest_paths.append(manifest_path)
        return items, current_artifacts, manifest_paths

    def _continuation_assembly_receipt(
        self,
        request: Mapping[str, Any],
        *,
        accepted: bool,
        assembly_manifest: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        receipt = self._blender_assembly_receipt(
            request,
            accepted=accepted,
            assembly_manifest=assembly_manifest,
        )
        binding = self._continuation_binding(request)
        receipt["continuation"] = {
            "parentOperationId": binding["parentOperationId"],
            "parentRequestSha256": binding["parentRequestSha256"],
            "parentResultSha256": binding["parentResultSha256"],
            "retainedBundleCount": len(binding["retainedItems"]),
            "retriedBundleCount": len(binding["retryIndices"]),
        }
        return receipt

    def _partial_blender_continuation_result(
        self,
        state: Mapping[str, Any],
        job_dir: Path,
        provider_error: str,
    ) -> dict[str, Any]:
        self._require_state_request_hash(state)
        self._verify_continuation_plan_input(state, job_dir)
        items, artifacts, unused_manifests = self._collect_blender_continuation_items(
            state, job_dir
        )
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("Blender continuation request is unavailable")
        binding = self._continuation_binding(request)
        accepted = sum(item.get("status") == "completed" for item in items)
        failed = len(items) - accepted
        return {
            "complete": False,
            "partial": accepted > 0,
            "inventoryId": binding["inventoryId"],
            "inventoryDecisionSha256": binding["inventoryDecisionSha256"],
            "exportOnly": bool(request["exportOnly"]),
            "geometrySource": "not-assembled",
            "bundleCount": accepted,
            "items": items,
            "artifacts": artifacts,
            "assemblyManifest": None,
            "assemblyReceipt": self._continuation_assembly_receipt(
                request, accepted=False, assembly_manifest=None
            ),
            "blend": None,
            "validation": None,
            "validationReceipt": None,
            "aggregate": {
                "status": "partial" if accepted else "failed",
                "requestedCount": len(items),
                "completedBundleCount": accepted,
                "failedItemCount": failed,
                "retainedBundleCount": sum(
                    item.get("status") == "completed" and item.get("retained") is True
                    for item in items
                ),
                "retriedBundleCount": sum(
                    item.get("status") == "completed" and item.get("retained") is False
                    for item in items
                ),
                "assemblyAccepted": False,
                "inventoryId": binding["inventoryId"],
                "inventoryDecisionSha256": binding[
                    "inventoryDecisionSha256"
                ],
            },
            "providerFailure": _redact(provider_error)[:4000],
        }

    def _partial_blender_result(
        self,
        state: Mapping[str, Any],
        job_dir: Path,
        provider_error: str,
    ) -> dict[str, Any]:
        items, artifacts = self._collect_blender_bundle_items(state, job_dir)
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("UEX Blender job request is unavailable")
        binding = self._blender_inventory_binding(request)
        accepted = sum(item.get("status") == "completed" for item in items)
        failed = len(items) - accepted
        aggregate_status = "partial" if accepted else "failed"
        return {
            "complete": False,
            "partial": accepted > 0,
            "inventoryId": binding["inventoryId"],
            "inventoryDecisionSha256": binding["inventoryDecisionSha256"],
            "exportOnly": bool(state["request"]["exportOnly"]),
            "geometrySource": "not-assembled",
            "bundleCount": accepted,
            "items": items,
            "artifacts": artifacts,
            "assemblyManifest": None,
            "assemblyReceipt": self._blender_assembly_receipt(
                request, accepted=False, assembly_manifest=None
            ),
            "blend": None,
            "validation": None,
            "validationReceipt": None,
            "aggregate": {
                "status": aggregate_status,
                "requestedCount": len(items),
                "completedBundleCount": accepted,
                "failedItemCount": failed,
                "assemblyAccepted": False,
                "inventoryId": binding["inventoryId"],
                "inventoryDecisionSha256": binding[
                    "inventoryDecisionSha256"
                ],
            },
            "providerFailure": _redact(provider_error)[:4000],
        }

    def _verify_blender(self, state: Mapping[str, Any], job_dir: Path) -> dict[str, Any]:
        target = job_dir / "target"
        blend = target / "UEX_Assembly.blend"
        assembly = blend.with_suffix(".assembly.json")
        if assembly.is_symlink() or not assembly.is_file():
            raise UnpackedAssetJobError("UEX Blender job omitted its assembly manifest")
        value = _read_json(assembly)
        if value.get("schema") != "uex.blender-assembly.v1":
            raise UnpackedAssetJobError("UEX Blender assembly schema is unsupported")
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("UEX Blender job request is unavailable")
        binding = self._blender_inventory_binding(request)
        expected_assets = list(request["virtualAssets"])
        if value.get("sourceAssets") != expected_assets:
            raise UnpackedAssetJobError("UEX Blender assembly changed the selected asset set")
        if value.get("profile") != request.get("profile"):
            raise UnpackedAssetJobError("UEX Blender assembly changed the selected profile")
        export_only = bool(request["exportOnly"])
        expected_status = "exported" if export_only else "complete"
        if value.get("status") != expected_status:
            raise UnpackedAssetJobError("UEX Blender assembly is not terminal-complete")
        options = value.get("options")
        if not isinstance(options, Mapping):
            raise UnpackedAssetJobError("UEX Blender assembly omitted its options binding")
        expected_options = {
            "lod0Only": bool(request["lod0Only"]),
            "exportMorphTargets": bool(request["exportMorphTargets"]),
            "exportOnly": export_only,
            "preferUeModel": bool(request["preferUeModel"]),
        }
        if any(options.get(key) is not expected for key, expected in expected_options.items()):
            raise UnpackedAssetJobError("UEX Blender assembly options changed after dispatch")
        if options.get("overwrite") is not False:
            raise UnpackedAssetJobError("UEX Blender assembly did not preserve no-overwrite policy")
        assembly_record = _managed_file(target, str(value.get("assemblyManifest", "") or ""), "assembly manifest")
        if assembly_record != assembly.resolve(strict=True):
            raise UnpackedAssetJobError("UEX Blender assembly path does not identify itself")

        expected_geometry = "not-assembled" if export_only else (
            "uemodel" if request["preferUeModel"] else "glb"
        )
        if value.get("geometrySource") != expected_geometry:
            raise UnpackedAssetJobError("UEX Blender geometry route was silently changed")
        bundles = value.get("bundles")
        if not isinstance(bundles, list) or len(bundles) != len(expected_assets):
            raise UnpackedAssetJobError("UEX Blender assembly omitted a selected bundle")
        items, bundle_artifacts = self._collect_blender_bundle_items(
            state, job_dir, bundles
        )
        failed_items = [item for item in items if item.get("status") != "completed"]
        if failed_items:
            raise UnpackedAssetJobError(
                "UEX Blender assembly contains an invalid selected bundle: %s"
                % failed_items[0].get("reason", "validation failed")
            )
        assembly_artifact = self._artifact(assembly, target)
        artifacts = [assembly_artifact]
        artifact_paths = {assembly_artifact["path"]}
        for artifact in bundle_artifacts:
            if artifact["path"] not in artifact_paths:
                artifact_paths.add(artifact["path"])
                artifacts.append(artifact)
        validation_record = None
        validation_receipt = None
        blend_record = None
        validation = blend.with_suffix(".validation.json")
        if export_only:
            if any(
                value.get(key) is not None
                for key in ("outputBlend", "outputSha256", "validationReport", "validationOk")
            ):
                raise UnpackedAssetJobError("bundle-only UEX export claimed a Blender assembly")
        else:
            output_path = _managed_file(target, str(value.get("outputBlend", "") or ""), "Blender output")
            validation_path = _managed_file(
                target, str(value.get("validationReport", "") or ""), "Blender validation"
            )
            if output_path != blend.resolve(strict=True) or validation_path != validation.resolve(strict=True):
                raise UnpackedAssetJobError("UEX Blender assembly output paths changed")
            if value.get("validationOk") is not True:
                raise UnpackedAssetJobError("UEX Blender assembly did not record passing validation")
            value_validation = _read_json(validation_path)
            if value_validation.get("schema") != "uex.blender-validation.v1" or value_validation.get("ok") is not True:
                raise UnpackedAssetJobError("UEX Blender validation did not pass")
            if str(value_validation.get("blendFile", "") or "") != str(blend.resolve(strict=True)):
                raise UnpackedAssetJobError("UEX Blender validation belongs to another .blend")
            errors = value_validation.get("errors")
            if not isinstance(errors, list) or errors:
                raise UnpackedAssetJobError("UEX Blender validation contains errors")
            checks = value_validation.get("checks")
            if not isinstance(checks, Mapping):
                raise UnpackedAssetJobError("UEX Blender validation omitted its checks")
            positive_checks = ("meshCount", "armatureObjectCount", "referencedArmatureCount", "boneCount")
            if any(
                isinstance(checks.get(key), bool)
                or not isinstance(checks.get(key), int)
                or checks.get(key) <= 0
                for key in positive_checks
            ):
                raise UnpackedAssetJobError("UEX Blender validation failed mesh/rig/bone checks")
            shared_armature = _bounded_text(checks.get("sharedArmature"), 256)
            if not shared_armature:
                raise UnpackedAssetJobError("UEX Blender validation did not prove a shared armature")
            lod_collections = checks.get("lodCollections")
            if not isinstance(lod_collections, Mapping) or not isinstance(lod_collections.get("0"), list) or not lod_collections.get("0"):
                raise UnpackedAssetJobError("UEX Blender validation did not prove LOD0")
            missing_images = checks.get("missingImages")
            if not isinstance(missing_images, list) or missing_images:
                raise UnpackedAssetJobError("UEX Blender validation has missing images")
            checked_meshes = checks.get("meshes")
            if not isinstance(checked_meshes, list) or not checked_meshes:
                raise UnpackedAssetJobError("UEX Blender validation omitted mesh checks")
            for checked in checked_meshes:
                if not isinstance(checked, Mapping) or any(
                    isinstance(checked.get(key), bool)
                    or not isinstance(checked.get(key), int)
                    or checked.get(key) <= 0
                    for key in ("vertices", "weightedVertices", "armatureModifiers")
                ):
                    raise UnpackedAssetJobError("UEX Blender validation found unskinned geometry")
            blend_record = self._artifact(blend, target)
            validation_record = self._artifact(validation_path, target)
            if blend_record["path"] not in artifact_paths:
                artifacts.append(blend_record)
                artifact_paths.add(blend_record["path"])
            if validation_record["path"] not in artifact_paths:
                artifacts.append(validation_record)
                artifact_paths.add(validation_record["path"])
            if value.get("outputSha256") != blend_record["sha256"]:
                raise UnpackedAssetJobError("UEX Blender assembly output hash changed")
            validation_receipt = {
                "schema": "uex.blender-validation.v1",
                "ok": True,
                "errors": 0,
                "meshCount": checks["meshCount"],
                "referencedArmatureCount": checks["referencedArmatureCount"],
                "boneCount": checks["boneCount"],
                "sharedArmature": shared_armature,
                "lod0CollectionCount": len(lod_collections["0"]),
                "missingImageCount": 0,
            }
        return {
            "complete": True,
            "partial": False,
            "inventoryId": binding["inventoryId"],
            "inventoryDecisionSha256": binding["inventoryDecisionSha256"],
            "exportOnly": export_only,
            "geometrySource": value.get("geometrySource"),
            "bundleCount": len(bundles),
            "items": items,
            "artifacts": artifacts,
            "assemblyManifest": artifacts[0],
            "assemblyReceipt": self._blender_assembly_receipt(
                request, accepted=True, assembly_manifest=artifacts[0]
            ),
            "blend": blend_record,
            "validation": validation_record,
            "validationReceipt": validation_receipt,
            "aggregate": {
                "status": "complete",
                "requestedCount": len(expected_assets),
                "completedBundleCount": len(items),
                "failedItemCount": 0,
                "assemblyAccepted": True,
                "inventoryId": binding["inventoryId"],
                "inventoryDecisionSha256": binding[
                    "inventoryDecisionSha256"
                ],
            },
        }

    def _verify_blender_continuation(
        self, state: Mapping[str, Any], job_dir: Path
    ) -> dict[str, Any]:
        self._require_state_request_hash(state)
        self._verify_continuation_plan_input(state, job_dir)
        target = job_dir / "target"
        blend = target / "UEX_Assembly.blend"
        assembly = blend.with_suffix(".assembly.json")
        if _is_redirected(assembly) or not assembly.is_file():
            raise UnpackedAssetJobError(
                "Blender continuation omitted its assembly manifest"
            )
        value = _read_json(assembly)
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise UnpackedAssetJobError("Blender continuation request is unavailable")
        binding = self._continuation_binding(request)
        assets = list(request["virtualAssets"])
        export_only = bool(request["exportOnly"])
        if (
            value.get("schema") != "uex.blender-assembly.v1"
            or value.get("status") != ("exported" if export_only else "complete")
            or value.get("profile") != request.get("profile")
            or value.get("sourceAssets") != assets
        ):
            raise UnpackedAssetJobError(
                "Blender continuation assembly changed its request binding"
            )
        options = value.get("options")
        expected_options = {
            "lod0Only": bool(request["lod0Only"]),
            "exportMorphTargets": bool(request["exportMorphTargets"]),
            "exportOnly": export_only,
            "preferUeModel": bool(request["preferUeModel"]),
        }
        if (
            not isinstance(options, Mapping)
            or any(options.get(key) is not expected for key, expected in expected_options.items())
            or options.get("overwrite") is not False
        ):
            raise UnpackedAssetJobError(
                "Blender continuation assembly options changed"
            )
        expected_geometry = "not-assembled" if export_only else (
            "uemodel" if request["preferUeModel"] else "glb"
        )
        if value.get("geometrySource") != expected_geometry:
            raise UnpackedAssetJobError(
                "Blender continuation geometry route changed"
            )
        assembly_record = _managed_file(
            target, value.get("assemblyManifest"), "continuation assembly manifest"
        )
        if assembly_record != assembly.resolve(strict=True):
            raise UnpackedAssetJobError(
                "Blender continuation assembly path does not identify itself"
            )
        continuation = value.get("continuation")
        expected_continuation = {
            "parentOperationId": binding["parentOperationId"],
            "parentRequestSha256": binding["parentRequestSha256"],
            "parentResultSha256": binding["parentResultSha256"],
            "retainedBundleCount": len(binding["retainedItems"]),
            "retriedBundleCount": len(binding["retryIndices"]),
        }
        if continuation != expected_continuation:
            raise UnpackedAssetJobError(
                "Blender continuation parent receipt binding changed"
            )

        items, current_artifacts, manifests = self._collect_blender_continuation_items(
            state, job_dir
        )
        failed = [item for item in items if item.get("status") != "completed"]
        if failed or any(manifest is None for manifest in manifests):
            raise UnpackedAssetJobError(
                "Blender continuation contains an invalid source bundle"
            )
        bundles = value.get("bundles")
        if not isinstance(bundles, list) or len(bundles) != len(assets):
            raise UnpackedAssetJobError(
                "Blender continuation assembly omitted a source bundle"
            )
        for index, (asset, raw_bundle, manifest_path) in enumerate(
            zip(assets, bundles, manifests)
        ):
            if not isinstance(raw_bundle, Mapping) or raw_bundle.get("asset") != asset:
                raise UnpackedAssetJobError(
                    "Blender continuation bundle order changed at index %d" % index
                )
            try:
                recorded_manifest = Path(str(raw_bundle.get("manifest", ""))).resolve(
                    strict=True
                )
                recorded_directory = Path(
                    str(raw_bundle.get("directory", ""))
                ).resolve(strict=True)
            except OSError as error:
                raise UnpackedAssetJobError(
                    "Blender continuation bundle path is unavailable"
                ) from error
            if (
                manifest_path is None
                or recorded_manifest != manifest_path
                or recorded_directory != manifest_path.parent
            ):
                raise UnpackedAssetJobError(
                    "Blender continuation bundle path binding changed"
                )

        assembly_artifact = self._artifact(assembly, target)
        artifacts = [assembly_artifact]
        artifact_paths = {assembly_artifact["path"]}
        for artifact in current_artifacts:
            if artifact["path"] not in artifact_paths:
                artifact_paths.add(artifact["path"])
                artifacts.append(artifact)
        blend_record = None
        validation_record = None
        validation_receipt = None
        validation = blend.with_suffix(".validation.json")
        if export_only:
            if any(
                value.get(key) is not None
                for key in (
                    "outputBlend",
                    "outputSha256",
                    "validationReport",
                    "validationOk",
                )
            ):
                raise UnpackedAssetJobError(
                    "bundle-only continuation claimed a Blender assembly"
                )
        else:
            output_path = _managed_file(
                target, value.get("outputBlend"), "continuation Blender output"
            )
            validation_path = _managed_file(
                target,
                value.get("validationReport"),
                "continuation Blender validation",
            )
            if (
                output_path != blend.resolve(strict=True)
                or validation_path != validation.resolve(strict=True)
                or value.get("validationOk") is not True
            ):
                raise UnpackedAssetJobError(
                    "Blender continuation output/validation binding changed"
                )
            validation_value = _read_json(validation_path)
            if (
                validation_value.get("schema") != "uex.blender-validation.v1"
                or validation_value.get("ok") is not True
                or str(validation_value.get("blendFile", "") or "")
                != str(blend.resolve(strict=True))
            ):
                raise UnpackedAssetJobError(
                    "Blender continuation validation did not pass"
                )
            errors = validation_value.get("errors")
            checks = validation_value.get("checks")
            if not isinstance(errors, list) or errors or not isinstance(checks, Mapping):
                raise UnpackedAssetJobError(
                    "Blender continuation validation receipt is incomplete"
                )
            for key in (
                "meshCount",
                "armatureObjectCount",
                "referencedArmatureCount",
                "boneCount",
            ):
                metric = checks.get(key)
                if isinstance(metric, bool) or not isinstance(metric, int) or metric <= 0:
                    raise UnpackedAssetJobError(
                        "Blender continuation validation failed rig checks"
                    )
            shared_armature = _bounded_text(checks.get("sharedArmature"), 256)
            lod_collections = checks.get("lodCollections")
            missing_images = checks.get("missingImages")
            checked_meshes = checks.get("meshes")
            if (
                not shared_armature
                or not isinstance(lod_collections, Mapping)
                or not isinstance(lod_collections.get("0"), list)
                or not lod_collections.get("0")
                or not isinstance(missing_images, list)
                or missing_images
                or not isinstance(checked_meshes, list)
                or not checked_meshes
            ):
                raise UnpackedAssetJobError(
                    "Blender continuation validation failed shared-rig/LOD/image checks"
                )
            for checked in checked_meshes:
                if not isinstance(checked, Mapping) or any(
                    isinstance(checked.get(key), bool)
                    or not isinstance(checked.get(key), int)
                    or checked.get(key) <= 0
                    for key in ("vertices", "weightedVertices", "armatureModifiers")
                ):
                    raise UnpackedAssetJobError(
                        "Blender continuation validation found unskinned geometry"
                    )
            blend_record = self._artifact(blend, target)
            validation_record = self._artifact(validation_path, target)
            if value.get("outputSha256") != blend_record["sha256"]:
                raise UnpackedAssetJobError(
                    "Blender continuation output hash changed"
                )
            for artifact in (blend_record, validation_record):
                if artifact["path"] not in artifact_paths:
                    artifact_paths.add(artifact["path"])
                    artifacts.append(artifact)
            validation_receipt = {
                "schema": "uex.blender-validation.v1",
                "ok": True,
                "errors": 0,
                "meshCount": checks["meshCount"],
                "referencedArmatureCount": checks["referencedArmatureCount"],
                "boneCount": checks["boneCount"],
                "sharedArmature": shared_armature,
                "lod0CollectionCount": len(lod_collections["0"]),
                "missingImageCount": 0,
            }

        return {
            "complete": True,
            "partial": False,
            "inventoryId": binding["inventoryId"],
            "inventoryDecisionSha256": binding["inventoryDecisionSha256"],
            "exportOnly": export_only,
            "geometrySource": expected_geometry,
            "bundleCount": len(items),
            "items": items,
            "artifacts": artifacts,
            "assemblyManifest": assembly_artifact,
            "assemblyReceipt": self._continuation_assembly_receipt(
                request, accepted=True, assembly_manifest=assembly_artifact
            ),
            "blend": blend_record,
            "validation": validation_record,
            "validationReceipt": validation_receipt,
            "aggregate": {
                "status": "complete",
                "requestedCount": len(items),
                "completedBundleCount": len(items),
                "failedItemCount": 0,
                "retainedBundleCount": sum(
                    item.get("retained") is True for item in items
                ),
                "retriedBundleCount": sum(
                    item.get("retained") is False for item in items
                ),
                "assemblyAccepted": True,
                "inventoryId": binding["inventoryId"],
                "inventoryDecisionSha256": binding[
                    "inventoryDecisionSha256"
                ],
            },
        }

    def _finish(self, operation_id: str, state: dict[str, Any], job_dir: Path, return_code: int) -> dict[str, Any]:
        state["exitCode"] = int(return_code)
        try:
            if return_code != 0:
                detail = _tail(job_dir / "stderr.log") or _tail(job_dir / "stdout.log")
                provider_error = "provider exited with code %d%s" % (
                    return_code,
                    ": " + detail[-4000:] if detail else "",
                )
                if state.get("kind") in {
                    "uex-blender-export",
                    BLENDER_CONTINUATION_KIND,
                }:
                    result = (
                        self._partial_blender_continuation_result(
                            state, job_dir, provider_error
                        )
                        if state.get("kind") == BLENDER_CONTINUATION_KIND
                        else self._partial_blender_result(
                            state, job_dir, provider_error
                        )
                    )
                    state["status"] = result["aggregate"]["status"]
                    state["result"] = result
                    # A validated partial is a terminal evidence receipt, not
                    # a top-level Host error envelope.  Keep the provider
                    # failure inside the result so AgentHost can admit the
                    # successful child artifacts and plan only the failed set.
                    state["error"] = "" if state["status"] == "partial" else _redact(provider_error)[:8000]
                    state["updatedUnixMs"] = int(time.time() * 1000)
                    _atomic_json(job_dir / "job.json", state)
                    return state
                raise UnpackedAssetJobError(provider_error)
            result = (
                self._verify_report(state, job_dir)
                if state["kind"] == "uex-asset-report"
                else self._verify_blender_continuation(state, job_dir)
                if state["kind"] == BLENDER_CONTINUATION_KIND
                else self._verify_blender(state, job_dir)
            )
            state["status"] = "completed"
            state["result"] = result
            state["error"] = ""
        except Exception as error:
            if state.get("kind") in {
                "uex-blender-export",
                BLENDER_CONTINUATION_KIND,
            }:
                try:
                    result = (
                        self._partial_blender_continuation_result(
                            state, job_dir, str(error)
                        )
                        if state.get("kind") == BLENDER_CONTINUATION_KIND
                        else self._partial_blender_result(state, job_dir, str(error))
                    )
                except Exception as receipt_error:
                    state["status"] = "failed"
                    state["result"] = None
                    state["error"] = _redact(str(receipt_error))[:8000]
                else:
                    state["status"] = result["aggregate"]["status"]
                    state["result"] = result
            else:
                state["status"] = "failed"
                state["result"] = None
            if not state.get("error"):
                state["error"] = (
                    ""
                    if state.get("status") == "partial"
                    else _redact(str(error))[:8000]
                )
        state["updatedUnixMs"] = int(time.time() * 1000)
        _atomic_json(job_dir / "job.json", state)
        return state

    def _public_state(self, state: Mapping[str, Any], job_dir: Path) -> dict[str, Any]:
        result = json.loads(_canonical_bytes(state).decode("utf-8"))
        result.pop("pid", None)
        if isinstance(state.get("result"), Mapping):
            result["resultSha256"] = self._result_sha256(state)
        result["managedJobPath"] = str(job_dir)
        result["stdoutTail"] = _tail(job_dir / "stdout.log") if state.get("status") in {"partial", "failed", "interrupted"} else ""
        result["stderrTail"] = _tail(job_dir / "stderr.log") if state.get("status") in {"partial", "failed", "interrupted"} else ""
        result["policy"] = {
            "terminal": state.get("status") in {"completed", "partial", "failed", "interrupted"},
            "retrySameOperation": state.get("status") == "completed",
            "sameOperationReturnsExistingReceipt": True,
            "successfulItemsImmutable": state.get("status") in {"completed", "partial"},
            "retryOnlyFailedItemsWithNewOperation": state.get("status") == "partial",
            "continuationAvailable": bool(
                state.get("status") == "partial"
                and state.get("kind")
                in {"uex-blender-export", BLENDER_CONTINUATION_KIND}
                and isinstance(state.get("result"), Mapping)
                and isinstance(state["result"].get("aggregate"), Mapping)
                and state["result"]["aggregate"].get("completedBundleCount", 0) > 0
                and state["result"]["aggregate"].get("assemblyAccepted") is False
            ),
            "continuationRequiresParentRequestAndResultHashes": True,
            "secretsRedacted": True,
            "managedOutputOnly": True,
        }
        return result

    def status(self, operation_id: str) -> dict[str, Any]:
        with self._guard:
            state = self._load(operation_id)
            job_dir = self._job_dir(operation_id, must_exist=True)
            running = self._running.get(operation_id)
            if state.get("status") == "running":
                if running is None:
                    try:
                        recovered = (
                            self._verify_report(state, job_dir)
                            if state["kind"] == "uex-asset-report"
                            else self._verify_blender_continuation(state, job_dir)
                            if state["kind"] == BLENDER_CONTINUATION_KIND
                            else self._verify_blender(state, job_dir)
                        )
                    except Exception as error:
                        if state.get("kind") in {
                            "uex-blender-export",
                            BLENDER_CONTINUATION_KIND,
                        }:
                            try:
                                partial = (
                                    self._partial_blender_continuation_result(
                                        state,
                                        job_dir,
                                        "provider process ownership was lost: %s"
                                        % error,
                                    )
                                    if state.get("kind")
                                    == BLENDER_CONTINUATION_KIND
                                    else self._partial_blender_result(
                                        state,
                                        job_dir,
                                        "provider process ownership was lost: %s"
                                        % error,
                                    )
                                )
                            except Exception as receipt_error:
                                state["status"] = "interrupted"
                                state["error"] = (
                                    "provider process ownership was lost and its receipt "
                                    "binding failed: %s" % _redact(str(receipt_error))[:4000]
                                )
                                state["result"] = None
                            else:
                                partial["recoveredAfterHostRestart"] = True
                                state["result"] = partial
                                if partial["aggregate"]["completedBundleCount"] > 0:
                                    state["status"] = "partial"
                                    state["error"] = ""
                                else:
                                    state["status"] = "interrupted"
                                    state["error"] = (
                                        "provider process ownership was lost; effect completion is uncertain"
                                    )
                        else:
                            state["status"] = "interrupted"
                            state["error"] = (
                                "provider process ownership was lost; effect completion is uncertain"
                            )
                    else:
                        recovered["recoveredAfterHostRestart"] = True
                        state["status"] = "completed"
                        state["result"] = recovered
                        state["error"] = ""
                    state["updatedUnixMs"] = int(time.time() * 1000)
                    _atomic_json(job_dir / "job.json", state)
                else:
                    return_code = running.process.poll()
                    if return_code is not None:
                        running.stdout.close()
                        running.stderr.close()
                        self._running.pop(operation_id, None)
                        state = self._finish(operation_id, state, job_dir, return_code)
            return self._public_state(state, job_dir)


__all__ = [
    "BLENDER_ASSEMBLY_RECEIPT_SCHEMA",
    "BLENDER_CONTINUATION_KIND",
    "BLENDER_CONTINUATION_PLAN_SCHEMA",
    "CAPABILITIES_SCHEMA",
    "DECISION_DISCOVERY_SCHEMA",
    "INVENTORY_ASSET_QUERY_SCHEMA",
    "JOB_ROOT_ENV",
    "JOB_SCHEMA",
    "UEX_PYTHON_ENV",
    "UEX_BLENDER_CONTINUATION_DRIVER_ENV",
    "UnpackedAssetJobError",
    "UnpackedAssetJobManager",
    "normalize_asset_report_spec",
]


def _main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != "--run-blender-continuation":
        print("error: this module exposes no caller-controlled command interface", file=sys.stderr)
        return 2
    try:
        _run_blender_continuation_plan(Path(arguments[1]))
    except Exception as error:
        print("error: %s" % _redact(str(error))[:8000], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
