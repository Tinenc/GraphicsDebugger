"""Fixed ``D3DCompile`` adapter for a pinned ``d3dcompiler_47.dll``.

The adapter exists so a portable ReplayLab bundle can compile HLSL to native
Shader Model 4/5 DXBC without locating ``fxc.exe`` or a Windows SDK at runtime.
It deliberately exposes only the workspace-derived input, output, entry point,
and exact target profile.  There is no include handler, macro table, shell,
caller-controlled compiler flag, or arbitrary DLL search.

The tool registry pins the adapter host/script and DLL.  The expected DLL hash
is repeated here so a standalone/frozen adapter also fails closed before
loading bytes that differ from the generated registry.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import re
import sys
from typing import Sequence


MAX_SOURCE_BYTES = 64 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,511}$")
_PROFILE_RE = re.compile(r"^(vs|ps|gs|hs|ds|cs)_(4|5)_([01])$")
_D3DCOMPILER_FILE_NAME = "d3dcompiler_47.dll"


class _ID3DBlob(ctypes.Structure):
    pass


class _ID3DBlobVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
        ("GetBufferPointer", ctypes.c_void_p),
        ("GetBufferSize", ctypes.c_void_p),
    ]


_ID3DBlob._fields_ = [("lpVtbl", ctypes.POINTER(_ID3DBlobVtbl))]
_BlobPointer = ctypes.POINTER(_ID3DBlob)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _absolute_regular_file(raw: str, label: str) -> Path:
    source = Path(raw)
    if not source.is_absolute():
        raise RuntimeError("%s must be an absolute path" % label)
    if source.is_symlink():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    path = source.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    return path


def _new_output_path(raw: str) -> Path:
    value = Path(raw)
    if not value.is_absolute():
        raise RuntimeError("output must be an absolute path")
    if value.exists() or value.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    parent_source = value.parent
    if parent_source.is_symlink():
        raise RuntimeError("output parent must be a regular directory")
    parent = parent_source.resolve(strict=True)
    if not parent.is_dir():
        raise RuntimeError("output parent must be a regular directory")
    output = parent / value.name
    if output.exists() or output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    return output


def _validate_entry_and_profile(entry: str, target: str) -> tuple[str, str]:
    entry_value = str(entry).strip()
    if not _ENTRY_RE.fullmatch(entry_value):
        raise RuntimeError("entry point must be one bounded HLSL identifier")
    target_value = str(target).strip().casefold()
    match = _PROFILE_RE.fullmatch(target_value)
    if match is None:
        raise RuntimeError(
            "target must be an exact Shader Model 4/5 DXBC stage_major_minor profile"
        )
    return entry_value, target_value


def _blob_bytes(blob: _BlobPointer) -> bytes:
    if not bool(blob):
        return b""
    vtable = blob.contents.lpVtbl.contents
    get_pointer = ctypes.WINFUNCTYPE(ctypes.c_void_p, _BlobPointer)(
        vtable.GetBufferPointer
    )
    get_size = ctypes.WINFUNCTYPE(ctypes.c_size_t, _BlobPointer)(
        vtable.GetBufferSize
    )
    size = int(get_size(blob))
    pointer = get_pointer(blob)
    if size and not pointer:
        raise RuntimeError("ID3DBlob returned a null buffer with non-zero size")
    return ctypes.string_at(pointer, size) if size else b""


def _release_blob(blob: _BlobPointer) -> None:
    if not bool(blob):
        return
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, _BlobPointer)(
        blob.contents.lpVtbl.contents.Release
    )
    release(blob)


def _compile_bytes(
    dll_path: Path,
    source: bytes,
    source_name: str,
    entry_point: str,
    target_profile: str,
) -> tuple[int, bytes, bytes]:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("D3DCompiler adapter is supported only on Windows")

    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32.  The
    # primary DLL is an absolute, hash-checked path; dependencies may come only
    # from that directory or System32, never CWD/PATH.
    library = ctypes.WinDLL(str(dll_path), winmode=0x00000900)
    compile_function = library.D3DCompile
    compile_function.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(_BlobPointer),
        ctypes.POINTER(_BlobPointer),
    ]
    compile_function.restype = ctypes.c_long

    source_buffer = ctypes.create_string_buffer(source, len(source) + 1)
    code = _BlobPointer()
    diagnostics = _BlobPointer()
    try:
        result = int(
            compile_function(
                ctypes.cast(source_buffer, ctypes.c_void_p),
                len(source),
                source_name.encode("utf-8", errors="strict"),
                None,  # no caller macros
                None,  # no include handler; #include therefore fails closed
                entry_point.encode("ascii", errors="strict"),
                target_profile.encode("ascii", errors="strict"),
                1,  # D3DCOMPILE_DEBUG preserves the compiled entry name for exact replay reflection.
                0,  # fixed Flags2
                ctypes.byref(code),
                ctypes.byref(diagnostics),
            )
        )
        diagnostic_bytes = _blob_bytes(diagnostics)
        code_bytes = _blob_bytes(code)
        return result, code_bytes, diagnostic_bytes
    finally:
        # The two outputs are independent COM objects and must both be released
        # on success, warning, compiler failure, and Python-side copy failure.
        _release_blob(code)
        _release_blob(diagnostics)


def _write_native_diagnostics(payload: bytes) -> None:
    if not payload:
        return
    # ID3DBlob text normally has one trailing NUL. Preserve every diagnostic
    # byte while omitting that C terminator from the terminal stream.
    visible = payload[:-1] if payload.endswith(b"\x00") else payload
    stream = getattr(sys.stderr, "buffer", None)
    if stream is not None:
        stream.write(visible)
        if visible and not visible.endswith((b"\n", b"\r")):
            stream.write(b"\n")
        stream.flush()
    else:  # pragma: no cover - normal CPython stderr always exposes buffer
        print(visible.decode("utf-8", errors="replace"), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", required=True)
    parser.add_argument("--dll-sha256", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--target", required=True)
    parsed = parser.parse_args(argv)

    if os.name != "nt":
        raise RuntimeError("D3DCompiler adapter is supported only on Windows")
    dll = _absolute_regular_file(parsed.dll, "d3dcompiler_47.dll")
    if dll.name.casefold() != _D3DCOMPILER_FILE_NAME:
        raise RuntimeError("compiler DLL must be named d3dcompiler_47.dll")
    expected_hash = str(parsed.dll_sha256).strip().casefold()
    if not _HASH_RE.fullmatch(expected_hash):
        raise RuntimeError("DLL SHA-256 must contain exactly 64 lowercase hex digits")
    if _sha256(dll) != expected_hash:
        raise RuntimeError("d3dcompiler_47.dll SHA-256 changed before adapter load")

    source_path = _absolute_regular_file(parsed.input, "input")
    source_size = source_path.stat().st_size
    if source_size <= 0 or source_size > MAX_SOURCE_BYTES:
        raise RuntimeError(
            "input HLSL must contain 1..%d bytes" % MAX_SOURCE_BYTES
        )
    output = _new_output_path(parsed.output)
    entry_point, target_profile = _validate_entry_and_profile(
        parsed.entry, parsed.target
    )
    source = source_path.read_bytes()
    hresult, code, diagnostics = _compile_bytes(
        dll,
        source,
        source_path.name,
        entry_point,
        target_profile,
    )
    _write_native_diagnostics(diagnostics)
    if hresult < 0:
        print(
            "D3DCompile failed with HRESULT 0x%08X" % (hresult & 0xFFFFFFFF),
            file=sys.stderr,
        )
        return 1
    if not code:
        raise RuntimeError("D3DCompile succeeded without producing a shader blob")
    with output.open("xb") as stream:
        stream.write(code)
        stream.flush()
        os.fsync(stream.fileno())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
