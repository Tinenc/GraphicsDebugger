"""Bind one successful UEX texture preview to its indexed original package.

This is a bounded content-cache update, never a material/GPU identity claim or a
permission to publish names. Missing inputs leave the reader receipt successful.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .asset_catalog import (
    MAX_READER_PREVIEW_BYTES,
    MAX_PREVIEW_INPUT_BYTES,
    AssetCatalogService,
    _hash_regular_file,
    _is_redirected,
)


class PreviewIndexUnavailable(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def unavailable(reason: str, detail: str = "") -> dict[str, Any]:
    return {"indexed": False, "reason": reason, "detail": detail[:512],
            "physicalIdentityVerified": False, "absenceProven": False}


def _identity(path: Path, *, maximum: int) -> dict[str, Any]:
    if any(_is_redirected(part) for part in (path, *path.parents)) or not path.is_file():
        raise PreviewIndexUnavailable("input-unavailable-or-redirected", str(path))
    size = path.stat().st_size
    if size > maximum:
        raise PreviewIndexUnavailable("input-byte-bound", str(path))
    digest = _hash_regular_file(path, size)
    if path.stat().st_size != size:
        raise PreviewIndexUnavailable("input-changed-during-read", str(path))
    return {"size": size, "sha256": digest}


def _closure(root: Path, relative: str) -> list[dict[str, Any]]:
    owner = root / relative
    if owner.suffix.lower() != ".uasset" or root not in owner.resolve().parents:
        raise PreviewIndexUnavailable("unsupported-source-package")
    # Check only exact companion names. Also check absent companions after the
    # reader: a newly created/replaced bulk payload invalidates this decode.
    paths = [owner.with_suffix(suffix) for suffix in
             (".uasset", ".uexp", ".ubulk", ".m.ubulk", ".uptnl")]
    result, total = [], 0
    for path in paths:
        if not path.exists() and not _is_redirected(path):
            if path == owner:
                raise PreviewIndexUnavailable("source-package-missing")
            continue
        identity = _identity(path, maximum=MAX_PREVIEW_INPUT_BYTES)
        total += identity["size"]
        if total > MAX_PREVIEW_INPUT_BYTES:
            raise PreviewIndexUnavailable("source-closure-byte-bound")
        result.append({"relativePath": path.relative_to(root).as_posix(), **identity})
    return result


@dataclass(frozen=True)
class PreviewInputs:
    root: Path
    reference: str
    relative_path: str
    source_files: list[dict[str, Any]]
    config_path: Path
    config_identity: dict[str, Any]
    mount_root: Path
    profile: str
    build_id: str


def prepare_preview(catalog: AssetCatalogService, root: Path, reference: str,
                    profile: Mapping[str, Any]) -> PreviewInputs:
    """Capture the original bytes before invoking the reader; never scan a tree."""
    root = root.resolve()
    if not reference or not catalog.database_path.is_file():
        raise PreviewIndexUnavailable("catalog-not-indexed")
    mount = Path(str(profile["mountRoot"])).absolute()
    if (not mount.is_dir() or any(_is_redirected(part) for part in (mount, *mount.parents))
            or (mount.resolve() != root and root not in mount.resolve().parents)):
        raise PreviewIndexUnavailable("reader-mount-unavailable-or-redirected")
    resolved = catalog.resolve_unreal_texture_paths(
        root, [reference], reader_mount_roots=[mount], hydrate_current=True)
    relative = resolved.get("resolved", {}).get(reference)
    if not relative:
        detail = "; ".join(str(item.get("reason", "")) for item in resolved.get("unavailable", []))
        raise PreviewIndexUnavailable("texture-reference-not-indexed", detail or str(resolved.get("reason", "")))
    sources = _closure(root, relative)
    # Reject a source changing between catalog resolution and the pre-run hash.
    rows = catalog.query(relative_paths=[relative], include_relationships=False, limit=1)["assets"]
    owner = next(item for item in sources if item["relativePath"] == relative)
    if not rows or rows[0]["contentSha256"] != owner["sha256"]:
        raise PreviewIndexUnavailable("source-changed-before-reader")
    config = Path(str(profile["configPath"])).absolute()
    # UEX package lookup accepts mount-relative package filenames, whereas
    # catalog relations commonly contain /Game/.../Texture.Texture object paths.
    # Resolve once above; never hand that object suffix to the package reader.
    try:
        reader_reference = (root / relative).resolve().relative_to(mount.resolve()).as_posix()
    except ValueError as error:
        raise PreviewIndexUnavailable("texture-outside-reader-mount") from error
    return PreviewInputs(root, reader_reference, relative, sources, config,
                         _identity(config, maximum=4 * 1024 * 1024),
                         mount, str(profile["profile"]), str(catalog.status().get("buildId", "")))


def register_preview(catalog: AssetCatalogService, inputs: PreviewInputs,
                     receipt: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
    """Cache only the PNG produced by this exact Host-run preview operation."""
    if receipt.get("succeeded") is not True or receipt.get("operation") != "uex_preview_texture":
        raise PreviewIndexUnavailable("reader-did-not-succeed")
    output = Path(str(receipt.get("outputDirectory", "")))
    if (not output.is_absolute() or output.parent.resolve() != output_root.resolve()
            or not re.fullmatch(r"run-[0-9a-f]{16}", output.name)
            or any(_is_redirected(part) for part in (output, *output.parents))):
        raise PreviewIndexUnavailable("output-directory-not-host-run")
    preview = output / "preview.png"
    argv = receipt.get("argv") or []
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise PreviewIndexUnavailable("reader-invocation-unbound")
    def option(name: str) -> str:
        if argv.count(name) != 1 or argv.index(name) + 1 >= len(argv):
            raise PreviewIndexUnavailable("reader-invocation-unbound")
        return argv[argv.index(name) + 1]
    if ("preview-texture" not in argv or not argv or argv[-1] != inputs.reference
            or Path(option("--out")).resolve() != preview.resolve()
            or Path(option("--config")).resolve() != inputs.config_path.resolve()
            or option("--profile") != inputs.profile):
        raise PreviewIndexUnavailable("reader-invocation-unbound")
    produced = [item for item in receipt.get("producedFiles", [])
                if isinstance(item, Mapping) and Path(str(item.get("path", ""))).resolve() == preview.resolve()]
    if len(produced) != 1:
        raise PreviewIndexUnavailable("preview-not-in-reader-produced-files")
    preview_identity = _identity(preview, maximum=MAX_READER_PREVIEW_BYTES)
    if produced[0].get("byteSize") != preview_identity["size"]:
        raise PreviewIndexUnavailable("preview-output-changed")
    if (_closure(inputs.root, inputs.relative_path) != inputs.source_files
            or _identity(inputs.config_path, maximum=4 * 1024 * 1024) != inputs.config_identity
            or not inputs.mount_root.is_dir()
            or any(_is_redirected(part) for part in (inputs.mount_root, *inputs.mount_root.parents))):
        raise PreviewIndexUnavailable("reader-inputs-changed")
    if str(catalog.status().get("buildId", "")) != inputs.build_id:
        raise PreviewIndexUnavailable("catalog-build-changed")
    receipt_sha = hashlib.sha256(json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    owner = next(item for item in inputs.source_files if item["relativePath"] == inputs.relative_path)
    return catalog.index_texture_preview(
        inputs.root, inputs.relative_path, preview,
        expected_preview_sha256=preview_identity["sha256"],
        expected_source_sha256=owner["sha256"],
        provenance={"operationId": "uex_preview_texture:" + output.name,
                    "receiptSha256": receipt_sha, "sourceFiles": inputs.source_files,
                    "colorSpace": "reader-unspecified", "channels": "reader-decoded-rgba"})
