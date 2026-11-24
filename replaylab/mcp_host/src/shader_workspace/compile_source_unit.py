"""Freeze and verify the exact include files consumed by one compiler job."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ManifestValidationError
from .io import atomic_write_bytes, is_within
from .source_documents import MAX_DOCUMENT_BYTES, MAX_UNIT_BYTES, MAX_UNIT_DOCUMENTS, capture_source_unit, rewrite_includes

SCHEMA = "bread-shader-compile-source-unit/v1"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _read_bounded(path: Path, maximum: int) -> bytes:
    if not 0 <= path.stat().st_size <= maximum:
        raise ValueError("source-unit artifact exceeds its byte limit")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("source-unit artifact exceeds its byte limit")
    return raw


def execution_context(root: Path, unit: Mapping[str, Any], proof: Mapping[str, Any] | None) -> dict[str, Any]:
    if proof is None:
        return {}
    return {"source_unit": {"workspaceRoot": root, "sourcePath": Path(unit["rootPath"]),
                            "source": unit["documents"][0]["raw"], "proof": proof}}


def capture_compilation_unit(source_path: Path, source: bytes) -> dict[str, Any]:
    # Preserve the existing single-file compiler size/language contract. The
    # tighter recursive-document bounds apply only when dependencies exist.
    if not re.search(br"(?im)^[ \t]*#[ \t]*include\b", source):
        return {"rootPath": str(source_path.absolute()), "digest": "", "documents": [
            {"path": str(source_path.absolute()), "raw": source, "sha256": _sha(source), "includes": []}]}
    return capture_source_unit(source_path, source_override=source)


def write_snapshot(root: Path, input_path: Path, unit: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep original bytes for audit and compile only rewritten frozen copies."""
    documents = unit["documents"]
    if (not unit["digest"] and not re.search(
            br"(?im)^[ \t]*#[ \t]*include\b", documents[0]["raw"])):
        atomic_write_bytes(input_path, documents[0]["raw"])
        return None
    directory = input_path.parent / "source-input"
    directory.mkdir(exist_ok=False)
    mapping = {item["path"]: (input_path if index == 0 else
               directory / ("%02d%s" % (index, Path(item["path"]).suffix)))
               for index, item in enumerate(documents)}
    records = []
    for index, document in enumerate(documents):
        frozen = mapping[document["path"]]
        rewritten = rewrite_includes(document, mapping)
        atomic_write_bytes(frozen, rewritten)
        record = {"sourcePath": document["path"], "sourceSha256": document["sha256"],
                  "inputPath": frozen.relative_to(root).as_posix(), "inputSha256": _sha(rewritten)}
        if index:
            original = directory / ("original-%02d%s" % (index, Path(document["path"]).suffix))
            atomic_write_bytes(original, document["raw"])
            record["originalPath"] = original.relative_to(root).as_posix()
        records.append(record)
    return {"schema": SCHEMA, "digest": unit["digest"], "documents": records,
            "inputPath": records[0]["inputPath"], "inputSha256": records[0]["inputSha256"]}


def validate_snapshot(root: Path, source_path: Path, source: bytes,
                      proof: Mapping[str, Any] | None) -> str:
    """Validate historical snapshots without consulting mutable current headers."""
    if proof is None:
        return _sha(source)
    try:
        if not isinstance(proof, Mapping) or set(proof) != {"schema", "digest", "documents", "inputPath", "inputSha256"}:
            raise ValueError("invalid source-unit record")
        documents = proof["documents"]
        if proof["schema"] != SCHEMA or not isinstance(documents, list) or not 1 <= len(documents) <= MAX_UNIT_DOCUMENTS:
            raise ValueError("invalid source-unit documents")
        input_path = _artifact(root, proof["inputPath"])
        job = input_path.parent
        if job.parent != (root / "jobs").resolve():
            raise ValueError("source-unit input is outside its compiler job")
        originals, mapping, total = {}, {}, 0
        for index, record in enumerate(documents):
            if not isinstance(record, Mapping) or set(record) != (
                    {"sourcePath", "sourceSha256", "inputPath", "inputSha256"} |
                    ({"originalPath"} if index else set())):
                raise ValueError("invalid source-unit document record")
            original_path = Path(record["sourcePath"])
            if not original_path.is_absolute() or _key(original_path) in originals:
                raise ValueError("duplicate or relative source-unit document")
            if index == 0 and _key(original_path) != _key(source_path):
                raise ValueError("source-unit root does not match working source")
            frozen = _artifact(root, record["inputPath"])
            if not is_within(job, frozen):
                raise ValueError("source-unit input escaped its compiler job")
            if index:
                original = _artifact(root, record["originalPath"])
                if not is_within(job, original):
                    raise ValueError("source-unit original escaped its compiler job")
                raw = _read_bounded(original, MAX_DOCUMENT_BYTES)
            else:
                raw = source
            total += len(raw)
            if total > MAX_UNIT_BYTES:
                raise ValueError("source-unit originals exceed the total byte limit")
            if _sha(raw) != record["sourceSha256"]:
                raise ValueError("source-unit original bytes changed")
            originals[_key(original_path)] = raw
            mapping[str(original_path)] = frozen
        def read_original(path: Path) -> bytes:
            if _key(path) not in originals:
                raise ValueError("source-unit include was not frozen")
            return originals[_key(path)]
        unit = capture_source_unit(source_path, source_override=source, document_reader=read_original)
        if (unit["digest"] != proof["digest"]
                or {_key(item["path"]) for item in unit["documents"]} != set(originals)):
            raise ValueError("source-unit dependency digest changed")
        for document, record in zip(unit["documents"], documents):
            if _key(document["path"]) != _key(record["sourcePath"]):
                raise ValueError("source-unit dependency order changed")
            rewritten = rewrite_includes(document, mapping)
            frozen = _artifact(root, record["inputPath"])
            if _read_bounded(frozen, MAX_UNIT_BYTES) != rewritten or _sha(rewritten) != record["inputSha256"]:
                raise ValueError("source-unit compiler input changed")
        if proof["inputPath"] != documents[0]["inputPath"] or proof["inputSha256"] != documents[0]["inputSha256"]:
            raise ValueError("source-unit compiler root binding changed")
        return proof["inputSha256"]
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise ManifestValidationError("compiled include snapshot is invalid: %s" % error) from error


def _artifact(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid source-unit artifact path")
    path = root / relative
    if not is_within(root, path.resolve()) or any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError("source-unit artifact is missing or redirected")
    return path.resolve()


def current_dependencies(source_path: Path, source: bytes, candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    """A changed/missing header makes the current editor source stale, not corrupt."""
    try:
        unit = capture_compilation_unit(source_path, source)
        digest = unit["digest"]
        expected = (candidate or {}).get("sourceDependencies", {}).get("digest", "")
        return {"digest": digest, "current": bool(candidate) and digest == expected,
                "documentCount": len(unit["documents"]), "error": ""}
    except (OSError, ValueError, TypeError) as error:
        return {"digest": "", "current": False, "documentCount": 0, "error": str(error)}
