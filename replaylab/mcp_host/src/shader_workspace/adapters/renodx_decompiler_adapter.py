"""Fail-closed adapter for RenoDX's DXIL-to-HLSL decompiler.

RenoDX reconstructs useful Shader Model 6 HLSL, but its current CLI always
names the entry point ``main`` and builds the HLSL parameter list from the
second DXIL signature-property table.  A packed input row that is present in
the physical signature but absent from that property table can therefore be
omitted.  The omission shifts later input registers when the reconstructed
source is compiled.

This adapter performs two deliberately mechanical operations:

* rename the single generated ``main`` definition to the capture entry name;
* validate the complete generated parameter list against the hash-bound
  workspace baseline interface and reorder those unchanged declarations into
  physical signature order.

It does not rewrite expressions, resources, outputs, constant buffers, or
control flow.  A missing or extra input row fails closed; the source-fixed
RenoDX binary owns signature reconstruction.  The workspace's untouched
compile and fresh replay-reflection gates remain authoritative after this
adapter succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence


MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_HLSL_BYTES = 64 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAIN = re.compile(
    r"(?m)^[ \t]*(?:[A-Za-z_][A-Za-z0-9_]*(?:<[^>\r\n]+>)?[ \t]+)+"
    r"(?P<name>main)[ \t]*\("
)
_SEMANTIC = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


class RenoDxAdapterError(RuntimeError):
    """The fixed RenoDX invocation or mechanical ABI repair is unsafe."""


def _regular_file(raw: str | Path, label: str) -> Path:
    source = Path(raw)
    if not source.is_absolute():
        raise RenoDxAdapterError(f"{label} must be absolute")
    if source.is_symlink():
        raise RenoDxAdapterError(f"{label} must be a regular non-symlink file")
    path = source.resolve(strict=True)
    if not path.is_file():
        raise RenoDxAdapterError(f"{label} must be a regular non-symlink file")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_json(path: Path) -> Mapping[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise RenoDxAdapterError("workspace manifest size is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenoDxAdapterError("workspace manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RenoDxAdapterError("workspace manifest root must be an object")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 1 << 30) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise RenoDxAdapterError(f"{label} is invalid")
    return value


def _semantic_names(row: Mapping[str, Any]) -> frozenset[str]:
    name = str(row.get("semanticName", "") or "").strip()
    indexed = str(row.get("semanticIdxName", "") or "").strip()
    index = _integer(row.get("semanticIndex"), "semanticIndex")
    needs_index = row.get("needSemanticIndex")
    if not name or not _IDENTIFIER.fullmatch(name):
        raise RenoDxAdapterError("baseline input semanticName is invalid")
    values: set[str] = set()
    if indexed:
        if not _IDENTIFIER.fullmatch(indexed):
            raise RenoDxAdapterError("baseline input semanticIdxName is invalid")
        values.add(indexed.casefold())
    if needs_index is True:
        values.add((name + str(index)).casefold())
        # DXC conventionally omits an explicit zero from source semantics.
        if index == 0:
            values.add(name.casefold())
    elif needs_index is False:
        values.add(name.casefold())
    else:
        raise RenoDxAdapterError("baseline needSemanticIndex is invalid")
    return frozenset(values)


def _canonical_semantic(row: Mapping[str, Any]) -> str:
    indexed = str(row.get("semanticIdxName", "") or "").strip()
    name = str(row.get("semanticName", "") or "").strip()
    index = _integer(row.get("semanticIndex"), "semanticIndex")
    needs_index = row.get("needSemanticIndex")
    if indexed and _IDENTIFIER.fullmatch(indexed):
        # Preserve the compiler-friendly omission of a semantic index zero.
        if needs_index is True and index == 0:
            return name
        return indexed
    if not _IDENTIFIER.fullmatch(name):
        raise RenoDxAdapterError("baseline input semantic cannot be emitted as HLSL")
    return name + (str(index) if needs_index is True and index else "")


def _split_parameters(value: str) -> list[str]:
    if not value.strip():
        return []
    result: list[str] = []
    start = 0
    round_depth = square_depth = angle_depth = 0
    for index, character in enumerate(value):
        if character == "(":
            round_depth += 1
        elif character == ")":
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]":
            square_depth -= 1
        elif character == "<":
            angle_depth += 1
        elif character == ">":
            angle_depth -= 1
        elif character == "," and not (round_depth or square_depth or angle_depth):
            item = value[start:index].strip()
            if not item:
                raise RenoDxAdapterError("RenoDX emitted an empty entry parameter")
            result.append(item)
            start = index + 1
        if min(round_depth, square_depth, angle_depth) < 0:
            raise RenoDxAdapterError("RenoDX entry parameter delimiters are unbalanced")
    if round_depth or square_depth or angle_depth:
        raise RenoDxAdapterError("RenoDX entry parameter delimiters are unbalanced")
    item = value[start:].strip()
    if not item:
        raise RenoDxAdapterError("RenoDX emitted a trailing empty entry parameter")
    result.append(item)
    return result


def _closing_parenthesis(source: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(source)):
        character = source[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        if depth < 0:
            break
    raise RenoDxAdapterError("RenoDX main parameter list is unbalanced")


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    register = _integer(row.get("regIndex"), "input regIndex")
    mask = _integer(row.get("regChannelMask"), "input regChannelMask", maximum=15)
    if mask <= 0:
        raise RenoDxAdapterError("baseline input register mask is empty")
    first_lane = (mask & -mask).bit_length() - 1
    return register, first_lane, _canonical_semantic(row).casefold()


def repair_renodx_hlsl(
    source: str,
    *,
    baseline_interface: Mapping[str, Any],
    entry_point: str,
    stage: str,
) -> str:
    """Rename the entry and reorder a complete, already reconstructed input list."""

    if not isinstance(source, str) or not source or len(source.encode("utf-8")) > MAX_HLSL_BYTES:
        raise RenoDxAdapterError("RenoDX HLSL size is invalid")
    if "\x00" in source:
        raise RenoDxAdapterError("RenoDX HLSL contains NUL")
    entry = str(entry_point).strip()
    if not _IDENTIFIER.fullmatch(entry):
        raise RenoDxAdapterError("capture entry point is not a valid HLSL identifier")
    normalised_stage = str(stage).strip().casefold()
    if normalised_stage not in {
        "vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"
    }:
        raise RenoDxAdapterError("capture shader stage is unsupported")
    if baseline_interface.get("interfaceComplete") is not True:
        raise RenoDxAdapterError("baseline physical interface is incomplete")
    rows = baseline_interface.get("inputSignature")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RenoDxAdapterError("baseline input signature is invalid")
    matches = list(_MAIN.finditer(source))
    if len(matches) != 1:
        raise RenoDxAdapterError("RenoDX HLSL must contain exactly one main definition")
    match = matches[0]
    name_start, name_end = match.span("name")
    opening = source.find("(", name_end, match.end())
    closing = _closing_parenthesis(source, opening)
    parameters = _split_parameters(source[opening + 1 : closing])

    # Compute/task/mesh system inputs are not represented by the ordinary
    # input signature table.  Renaming remains safe, but no parameter repair is
    # attempted for those stages.
    if normalised_stage in {"compute", "task", "mesh"}:
        if rows:
            raise RenoDxAdapterError("non-graphics shader unexpectedly has an input signature")
        return source[:name_start] + entry + source[name_end:]

    semantic_to_row: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        names = _semantic_names(row)
        for name in names:
            previous = semantic_to_row.get(name)
            if previous is not None and previous is not row:
                raise RenoDxAdapterError("baseline input semantics are ambiguous")
            semantic_to_row[name] = row

    declarations: dict[int, str] = {}
    present: set[int] = set()
    for declaration in parameters:
        semantic_match = _SEMANTIC.search(declaration)
        if semantic_match is None:
            raise RenoDxAdapterError("RenoDX entry parameter has no simple semantic")
        row = semantic_to_row.get(semantic_match.group(1).casefold())
        if row is None:
            raise RenoDxAdapterError("RenoDX emitted an input absent from the baseline interface")
        identity = id(row)
        if identity in present:
            raise RenoDxAdapterError("RenoDX emitted a duplicate input semantic")
        present.add(identity)
        declarations[identity] = declaration

    ordered: list[tuple[Mapping[str, Any], str]] = []
    for row in sorted(rows, key=_row_sort_key):
        identity = id(row)
        declaration = declarations.get(identity)
        if declaration is None:
            raise RenoDxAdapterError("RenoDX omitted a baseline input signature row")
        ordered.append((row, declaration))

    if len(ordered) != len(rows):
        raise RenoDxAdapterError("baseline input signature reconstruction is incomplete")
    rendered = "\n" + ",\n".join(f"  {declaration}" for _, declaration in ordered) + "\n"
    return (
        source[:name_start]
        + entry
        + source[name_end : opening + 1]
        + rendered
        + source[closing:]
    )


def _baseline_interface(
    manifest: Mapping[str, Any], *, input_sha256: str, stage: str, entry_point: str
) -> Mapping[str, Any]:
    if manifest.get("stage") != stage or manifest.get("entryPoint") != entry_point:
        raise RenoDxAdapterError("workspace stage/entry binding contradicts the adapter request")
    baseline = manifest.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("rawSha256") != input_sha256:
        raise RenoDxAdapterError("RenoDX input is not the hash-bound workspace baseline")
    interface = baseline.get("compiledInterface")
    if not isinstance(interface, dict) or interface.get("entryPoint") != entry_point:
        raise RenoDxAdapterError("workspace baseline interface binding is invalid")
    return interface


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--tool-sha256", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--raw-sha256", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--stage", required=True)
    arguments = parser.parse_args(argv)

    tool = _regular_file(arguments.tool, "RenoDX decompiler")
    expected_tool_hash = str(arguments.tool_sha256).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_tool_hash) or _sha256(tool) != expected_tool_hash:
        raise RenoDxAdapterError("RenoDX decompiler SHA-256 mismatch")
    source = _regular_file(arguments.input, "input")
    input_hash = _sha256(source)
    expected_input_hash = str(arguments.raw_sha256).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_input_hash) or input_hash != expected_input_hash:
        raise RenoDxAdapterError("RenoDX input SHA-256 contradicts the workspace baseline")
    workspace = Path(arguments.workspace)
    if not workspace.is_absolute() or workspace.is_symlink():
        raise RenoDxAdapterError("workspace must be an absolute regular directory")
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise RenoDxAdapterError("workspace must be an absolute regular directory")
    manifest_path = _regular_file(workspace / "manifest.json", "workspace manifest")
    manifest = _bounded_json(manifest_path)
    interface = _baseline_interface(
        manifest,
        input_sha256=input_hash,
        stage=arguments.stage,
        entry_point=arguments.entry,
    )

    raw_output = Path(arguments.output)
    if not raw_output.is_absolute() or raw_output.is_symlink():
        raise RenoDxAdapterError("output must be an absolute new file")
    output = raw_output.resolve()
    if output.exists() or output.parent.is_symlink() or not output.parent.is_dir():
        raise RenoDxAdapterError("output must be a new file in a regular directory")
    decompiler_output = output.with_name(output.name + ".renodx-raw.hlsl")
    if decompiler_output.exists() or decompiler_output.is_symlink():
        raise RenoDxAdapterError("refusing to overwrite RenoDX raw evidence")

    completed = subprocess.run(
        [str(tool), str(source), str(decompiler_output)],
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        env=dict(os.environ),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return int(completed.returncode or 1)
    if (
        decompiler_output.is_symlink()
        or not decompiler_output.is_file()
        or decompiler_output.stat().st_size <= 0
        or decompiler_output.stat().st_size > MAX_HLSL_BYTES
    ):
        raise RenoDxAdapterError("RenoDX did not produce a bounded HLSL file")
    try:
        original = decompiler_output.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise RenoDxAdapterError("RenoDX HLSL is not strict UTF-8") from exc
    repaired = repair_renodx_hlsl(
        original,
        baseline_interface=interface,
        entry_point=arguments.entry,
        stage=arguments.stage,
    )
    encoded = repaired.encode("utf-8")
    if not encoded or len(encoded) > MAX_HLSL_BYTES:
        raise RenoDxAdapterError("repaired RenoDX HLSL size is invalid")
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RenoDxAdapterError("refusing to overwrite adapter temporary output")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    print(
        "RenoDX raw SHA-256 %s; mechanical HLSL SHA-256 %s"
        % (hashlib.sha256(original.encode("utf-8")).hexdigest(), hashlib.sha256(encoded).hexdigest())
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
