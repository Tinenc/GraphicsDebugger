"""Bounded parent-chain resolution for cooked Unreal material ShaderMaps.

The low-level UEX operation intentionally reads one exact loose package.  This
module composes those exact reads: when an instance owns no inline ShaderMap it
uses the parent and overrides embedded by that same exact-package read, resolves
the observed ``Parent`` object path inside the *already configured* unpacked
root, and repeats until it finds the material that owns compiled variants.
Older external readers without embedded preview evidence fall back to one
ordinary preview.

No directory search and no parent-directory widening happens here.  Every
derived package path must name an existing regular ``.uasset`` below both the
configured asset root and UEX mount root.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

MAX_CHAIN_DEPTH = 8
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_MATERIAL_TYPES = {
    "Material",
    "MaterialInstance",
    "MaterialInstanceConstant",
}


class MaterialParentChainError(ValueError):
    """A receipt or observed parent reference cannot be consumed safely."""


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(str(root.resolve())), os.path.normcase(str(candidate.resolve()))]
        ) == os.path.normcase(str(root.resolve()))
    except (OSError, ValueError):
        return False


def receipt_document(receipt: Mapping[str, Any]) -> Any:
    """Return complete JSON from one Host-owned tool receipt.

    UEX output larger than the inline preview is retained in ``stdoutPath``.
    Verify the bounded file against the receipt before parsing it.
    """

    if receipt.get("succeeded") is not True:
        return None
    path_text = receipt.get("stdoutPath")
    if path_text:
        if receipt.get("stdoutFileTruncated") is not False:
            raise MaterialParentChainError("UEX stdout file is truncated")
        size = receipt.get("stdoutFileByteSize")
        digest = str(receipt.get("stdoutFileSha256") or "").lower()
        output_text = receipt.get("outputDirectory")
        if (
            type(size) is not int
            or not 0 <= size <= MAX_RECEIPT_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(output_text, str)
            or not output_text
        ):
            raise MaterialParentChainError("UEX stdout file lacks bounded size/hash evidence")
        output_root = Path(output_text).resolve()
        target = Path(str(path_text)).resolve()
        if not output_root.is_absolute() or not target.is_absolute() or not _inside(output_root, target):
            raise MaterialParentChainError("UEX stdout file escapes its Host output directory")
        if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
            raise MaterialParentChainError("UEX stdout file is missing, redirected or changed")
        raw = target.read_bytes()
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise MaterialParentChainError("UEX stdout file differs from its receipt")
    else:
        stdout = receipt.get("stdout")
        if not isinstance(stdout, str) or receipt.get("stdoutTruncated") is True:
            raise MaterialParentChainError("UEX inline stdout is incomplete")
        raw = stdout.encode("utf-8")
        if len(raw) > MAX_RECEIPT_BYTES:
            raise MaterialParentChainError("UEX inline stdout exceeds its bound")
        declared_size = receipt.get("stdoutByteSize")
        declared_sha = receipt.get("stdoutSha256")
        if declared_size is not None and declared_size != len(raw):
            raise MaterialParentChainError("UEX inline stdout size differs from its receipt")
        if declared_sha is not None and declared_sha != hashlib.sha256(raw).hexdigest():
            raise MaterialParentChainError("UEX inline stdout hash differs from its receipt")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise MaterialParentChainError("UEX stdout is not complete UTF-8 JSON") from error


def _material_export(document: Any, asset_path: str) -> Mapping[str, Any] | None:
    records = document if isinstance(document, list) else [document]
    materials = [
        item
        for item in records
        if isinstance(item, Mapping) and str(item.get("Type") or "") in _MATERIAL_TYPES
    ]
    stem = PurePosixPath(asset_path).stem.casefold()
    exact = [item for item in materials if str(item.get("Name") or "").casefold() == stem]
    if len(exact) == 1:
        return exact[0]
    return materials[0] if len(materials) == 1 else None


def _parent_reference(document: Any, asset_path: str) -> str:
    material = _material_export(document, asset_path)
    if material is None:
        return ""
    properties = material.get("Properties")
    parent = properties.get("Parent") if isinstance(properties, Mapping) else None
    return str(parent.get("ObjectPath") or "").strip() if isinstance(parent, Mapping) else ""


def _embedded_material_preview(document: Any, asset_path: str) -> Any:
    """Return the ordinary export embedded by the exact-package ShaderMap reader.

    New UEX receipts carry the already-deserialized material export alongside the
    compiled metadata.  Older/external readers may not, so callers retain the
    proved ``uex_preview_asset`` fallback rather than weakening evidence.
    """

    if not isinstance(document, Mapping):
        return None
    stem = PurePosixPath(asset_path).stem.casefold()
    matches = []
    for item in document.get("materials") or []:
        if not isinstance(item, Mapping):
            continue
        preview = item.get("materialPreview")
        if (
            isinstance(preview, Mapping)
            and str(preview.get("Name") or item.get("objectName") or "").casefold() == stem
        ):
            matches.append(preview)
    return matches[0] if len(matches) == 1 else None


def _shader_map_variant_count(document: Any) -> int:
    if not isinstance(document, Mapping):
        return 0
    if (
        document.get("status") != "parsed"
        or (document.get("readShaderMaps") or {}).get("completed") is not True
        or (document.get("coverage") or {}).get("complete") is not True
    ):
        return 0
    count = 0
    for material in document.get("materials") or []:
        if isinstance(material, Mapping) and isinstance(material.get("variants"), list):
            count += len(material["variants"])
    return count


def _shader_map_status(document: Any) -> str:
    return str(document.get("status") or "") if isinstance(document, Mapping) else ""


def _evidence(receipt: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "path": str(receipt.get("stdoutPath") or ""),
        "sha256": str(receipt.get("stdoutFileSha256") or receipt.get("stdoutSha256") or ""),
        "byteSize": int(receipt.get("stdoutFileByteSize") or receipt.get("stdoutByteSize") or 0),
    }
    return {key: value for key, value in result.items() if value not in ("", 0)}


def _object_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    # Accept CUE4Parse's plain ObjectPath and standard Class'/Game/Path.Asset'
    # notation, but never interpret a quote as filesystem syntax.
    if "'" in text:
        pieces = text.split("'")
        text = pieces[1] if len(pieces) >= 3 else ""
    text = text.split(":", 1)[0]
    head, slash, tail = text.rpartition("/")
    if "." in tail:
        tail = tail.split(".", 1)[0]
    text = (head + slash + tail).strip("/")
    if (
        not text
        or len(text) > 2048
        or any(part in ("", ".", "..") for part in text.split("/"))
        or any(character in text for character in ("*", "?", "[", "]", ":", "\x00"))
    ):
        raise MaterialParentChainError("material Parent is not a bounded Unreal object path")
    return text


def resolve_parent_asset(
    parent_reference: str,
    *,
    asset_root: Path,
    mount_root: Path,
    game_name: str,
) -> tuple[str, Path]:
    """Map an observed Unreal Parent reference to one admitted loose package."""

    normalized = _object_path(parent_reference)
    lower = normalized.casefold()
    if lower.startswith("game/"):
        if not game_name:
            raise MaterialParentChainError(
                "Parent uses /Game but the configured asset root is a content subtree; "
                "configure the already authorized complete unpacked root containing <Game>/Content"
            )
        relative = f"{game_name}/Content/{normalized[len('Game/') :]}"
    elif lower.startswith("engine/"):
        relative = "Engine/Content/" + normalized[len("Engine/") :]
    else:
        relative = normalized
    relative = PurePosixPath(relative).with_suffix(".uasset").as_posix()
    target = (mount_root / Path(*PurePosixPath(relative).parts)).resolve()
    configured = asset_root.resolve()
    mounted = mount_root.resolve()
    if not _inside(mounted, target) or not _inside(configured, target):
        raise MaterialParentChainError(
            "observed material Parent is outside the configured unpacked asset root"
        )
    if target.is_symlink() or not target.is_file():
        raise MaterialParentChainError(
            "observed material Parent has no exact loose .uasset in the configured root"
        )
    return relative, target


def normalize_requested_asset(
    asset: str,
    *,
    asset_root: Path,
    mount_root: Path,
    game_name: str,
) -> str:
    """Normalize one exact admitted path to the UEX mount-relative spelling.

    Asset resolution commonly returns a path relative to the configured root
    (for example ``raw/Game/Content/...``), while UEX keys it relative to the
    detected mount root (``Game/Content/...``).  Resolve only those two exact
    interpretations; never search by basename or walk directories.
    """

    text = str(asset or "").strip().replace("\\", "/")
    if text.startswith("/Game/") or text.startswith("/Engine/"):
        relative, unused_target = resolve_parent_asset(
            text,
            asset_root=asset_root,
            mount_root=mount_root,
            game_name=game_name,
        )
        return relative
    if (
        not text
        or len(text) > 4096
        or any(part in ("", ".", "..") for part in text.split("/"))
        or any(character in text for character in ("*", "?", "[", "]", "\x00"))
    ):
        raise MaterialParentChainError("asset must be one exact in-root package path")
    configured = asset_root.resolve()
    mounted = mount_root.resolve()
    declared = Path(text)
    candidates: list[Path] = []
    if declared.is_absolute():
        candidates.append(declared.resolve())
    else:
        candidates.extend([
            (mounted / Path(*PurePosixPath(text).parts)).resolve(),
            (configured / Path(*PurePosixPath(text).parts)).resolve(),
        ])
    matches: list[Path] = []
    for candidate in candidates:
        if (
            _inside(configured, candidate)
            and _inside(mounted, candidate)
            and candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix.casefold() == ".uasset"
            and all(os.path.normcase(str(candidate)) != os.path.normcase(str(item))
                    for item in matches)
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise MaterialParentChainError(
            "asset must identify one exact loose .uasset inside the configured mount root"
        )
    return matches[0].relative_to(mounted).as_posix()


async def follow_material_parent_chain(
    requested_asset: str,
    initial_receipt: Mapping[str, Any],
    *,
    run_operation: Callable[[str, dict[str, Any], float], Awaitable[Mapping[str, Any]]],
    asset_root: Path,
    mount_root: Path,
    game_name: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Compose exact UEX reads and return the compiled ShaderMap owner receipt."""

    current_asset = str(requested_asset or "").replace("\\", "/")
    current_receipt = dict(initial_receipt)
    chain: list[dict[str, Any]] = []
    override_chain: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    visited: set[str] = set()

    for depth in range(MAX_CHAIN_DEPTH):
        shader_document = receipt_document(current_receipt)
        if isinstance(shader_document, Mapping):
            current_asset = str(
                (shader_document.get("asset") or {}).get("path") or current_asset
            ).replace("\\", "/")
        key = current_asset.casefold()
        if not key or key in visited:
            diagnostics.append({"code": "parent-cycle", "message": "material Parent chain repeats an asset"})
            break
        visited.add(key)
        variants = _shader_map_variant_count(shader_document)
        entry: dict[str, Any] = {
            "depth": depth,
            "assetPath": current_asset,
            "shaderMapStatus": _shader_map_status(shader_document),
            "variantCount": variants,
            "shaderMapEvidence": _evidence(current_receipt),
        }
        chain.append(entry)
        if variants:
            result = dict(current_receipt)
            result.update({
                "requestedAsset": str(requested_asset or ""),
                "followedParentChain": depth > 0,
                "materialChain": chain,
                "materialOverrideChain": override_chain,
                "shaderMapOwner": {
                    "assetPath": current_asset,
                    "depth": depth,
                    "variantCount": variants,
                    **_evidence(current_receipt),
                },
                "chainDiagnostics": diagnostics,
            })
            return result

        preview_document = _embedded_material_preview(shader_document, current_asset)
        if preview_document is not None:
            # Reuse the same immutable exact-package receipt.  This avoids mounting
            # an 800k-file loose tree for every parent while preserving the complete
            # serialized Parent/override properties and their input hashes.
            preview_receipt = current_receipt
            entry["previewSource"] = "shader-map-exact-package"
        else:
            preview_receipt = await run_operation(
                "uex_preview_asset", {"asset": current_asset}, timeout_seconds
            )
            preview_document = receipt_document(preview_receipt)
            entry["previewSource"] = "uex-preview-fallback"
        entry["previewEvidence"] = _evidence(preview_receipt)
        parent = _parent_reference(preview_document, current_asset)
        entry["parentReference"] = parent
        if not parent:
            diagnostics.append({
                "code": "no-parent-or-unreadable-preview",
                "message": "material owns no compiled variants and exposes no readable Parent",
            })
            break
        override_evidence = _evidence(preview_receipt)
        if override_evidence.get("path") and override_evidence.get("sha256"):
            override_chain.append({
                "path": override_evidence["path"],
                "sha256": override_evidence["sha256"],
                "assetPath": current_asset,
            })
        try:
            parent_asset, unused_path = resolve_parent_asset(
                parent,
                asset_root=asset_root,
                mount_root=mount_root,
                game_name=game_name,
            )
        except MaterialParentChainError as error:
            diagnostics.append({"code": "parent-out-of-scope", "message": str(error)})
            break
        entry["parentAssetPath"] = parent_asset
        current_asset = parent_asset
        current_receipt = dict(await run_operation(
            "uex_material_shader_maps", {"asset": current_asset}, timeout_seconds
        ))
    else:
        diagnostics.append({
            "code": "chain-depth-limit",
            "message": f"material Parent chain exceeds {MAX_CHAIN_DEPTH} packages",
        })

    result = dict(initial_receipt)
    result.update({
        "requestedAsset": str(requested_asset or ""),
        "followedParentChain": len(chain) > 1,
        "materialChain": chain,
        "materialOverrideChain": override_chain,
        "shaderMapOwner": None,
        "chainDiagnostics": diagnostics,
    })
    return result


__all__ = [
    "MAX_CHAIN_DEPTH",
    "MaterialParentChainError",
    "follow_material_parent_chain",
    "normalize_requested_asset",
    "receipt_document",
    "resolve_parent_asset",
]
