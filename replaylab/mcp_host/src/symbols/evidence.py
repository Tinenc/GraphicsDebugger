"""Bounded, deterministic file context for AI candidate review."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Iterable

from .store import SymbolStore


ASCII_STRING = re.compile(rb"[\x20-\x7e]{4,}")
UTF16LE_STRING = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
TOKEN = re.compile(r"[A-Za-z0-9_]{3,}")
MAX_WINDOW = 512 * 1024
MAX_STRINGS = 200
MAX_STRING_LENGTH = 300


def _inside(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except (OSError, ValueError):
        return False


def _source_path(root: Path, asset: dict) -> tuple[Path | None, str]:
    relative = str(asset.get("relative_path", "") or "")
    candidate = (root / relative).resolve()
    if _inside(root, candidate) and candidate.is_file():
        return candidate, "asset"

    source = asset.get("metadata", {}).get("_symbolSource", {})
    manifest = str(source.get("manifest", "") or "")
    if manifest:
        candidate = (root / manifest).resolve()
        if _inside(root, candidate) and candidate.is_file():
            return candidate, "manifest"
    return None, "missing"


def _strings(matches: Iterable[bytes], encoding: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for match in matches:
        try:
            value = match.decode(encoding, errors="strict").strip("\x00 \t\r\n")
        except UnicodeError:
            continue
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value[:MAX_STRING_LENGTH])
        if len(result) >= MAX_STRINGS:
            break
    return result


def _bounded_window(path: Path, asset: dict) -> dict:
    file_size = path.stat().st_size
    offset = max(int(asset.get("file_offset", 0)), 0)
    byte_size = max(int(asset.get("byte_size", 0)), 0)
    if byte_size:
        padding = max((MAX_WINDOW - min(byte_size, MAX_WINDOW)) // 2, 0)
        start = max(offset - padding, 0)
        length = min(MAX_WINDOW, file_size - start)
    else:
        start = 0
        length = min(MAX_WINDOW, file_size)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read(length)
    ascii_values = _strings((m.group(0) for m in ASCII_STRING.finditer(data)), "ascii")
    utf16_values = _strings(
        (m.group(0) for m in UTF16LE_STRING.finditer(data)), "utf-16-le"
    )
    return {
        "fileSize": file_size,
        "windowOffset": start,
        "windowSize": len(data),
        "windowSha256": hashlib.sha256(data).hexdigest(),
        "asciiStrings": ascii_values,
        "utf16LeStrings": utf16_values,
        "truncated": file_size > len(data),
    }


def _related_files(asset: dict, scan_summary: dict) -> list[dict]:
    source = "%s %s" % (asset.get("display_name", ""), asset.get("relative_path", ""))
    tokens = {
        token.lower()
        for token in TOKEN.findall(source)
        if token.lower() not in ("shader", "material", "asset", "game")
    }
    result: list[dict] = []
    for relative in scan_summary.get("textShaderFiles", []):
        lower = str(relative).lower()
        score = sum(1 for token in tokens if token in lower)
        if score:
            result.append({"relativePath": relative, "tokenScore": score})
    result.sort(key=lambda item: (-item["tokenScore"], item["relativePath"]))
    return result[:30]


def collect_asset_evidence(store: SymbolStore, asset: dict) -> dict:
    scan = store.get_asset_scan(asset["scan_id"])
    if scan is None:
        raise ValueError("Asset scan no longer exists")
    root = Path(scan["root_path"]).resolve()
    path, source_kind = _source_path(root, asset)
    file_context = {"available": False, "sourceKind": source_kind}
    if path is not None:
        try:
            file_context = {
                "available": True,
                "sourceKind": source_kind,
                "relativePath": str(path.relative_to(root)),
                **_bounded_window(path, asset),
            }
        except OSError as error:
            file_context = {
                "available": False,
                "sourceKind": source_kind,
                "error": str(error),
            }
    return {
        "assetShaderId": int(asset["id"]),
        "assetPath": asset.get("relative_path", ""),
        "displayName": asset.get("display_name", ""),
        "provenance": asset.get("provenance", ""),
        "rawSha256": asset.get("raw_sha256", ""),
        "codeSha256": asset.get("code_sha256", ""),
        "layoutHash": asset.get("layout_hash", ""),
        "metadata": asset.get("metadata", {}),
        "fileContext": file_context,
        "relatedTextShaders": _related_files(asset, scan["summary"]),
    }
