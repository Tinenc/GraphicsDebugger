"""Fixed ``IDxcAssembler`` adapter for pinned DXIL LLVM text.

The adapter exposes only an absolute SHA-256-pinned ``dxcompiler.dll`` plus
workspace-owned input and output paths.  It never searches ``PATH``, invokes a
shell, accepts a command line from a caller, downloads a binary, or resolves
includes.  The COM ABI constants and vtable slots below come from Microsoft's
``include/dxc/dxcapi.h``:

* ``CLSID_DxcLibrary`` / ``IID_IDxcLibrary`` create a copied UTF-8 input blob;
* ``CLSID_DxcAssembler`` / ``IID_IDxcAssembler`` expose one method after
  ``IUnknown``: ``AssembleToContainer``;
* ``IDxcOperationResult`` exposes ``GetStatus``, ``GetResult`` and
  ``GetErrorBuffer`` immediately after ``IUnknown``.

Assembly produces only a DXIL container candidate.  ReplayLab's normal fresh
reflection and ABI validation remain mandatory before a candidate can apply.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import re
import struct
import sys
from typing import Sequence


MAX_IR_BYTES = 64 * 1024 * 1024
MAX_CONTAINER_BYTES = 512 * 1024 * 1024
MAX_CONTAINER_PARTS = 4096
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DXIL_TRIPLE_RE = re.compile(
    rb'(?im)^\s*target\s+triple\s*=\s*"dxil(?:-[^"]*)?"\s*$'
)
_DXCOMPILER_FILE_NAME = "dxcompiler.dll"
_HRESULT = ctypes.c_int32
_ULONG = ctypes.c_uint32
_UINT32 = ctypes.c_uint32
_COM = ctypes.c_void_p
_STDCALL = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(
    data1: int,
    data2: int,
    data3: int,
    data4: tuple[int, int, int, int, int, int, int, int],
) -> _GUID:
    return _GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))


# Microsoft dxcapi.h identities. Keep these explicit so a header update cannot
# silently change which COM implementation the fixed adapter requests.
CLSID_DXC_LIBRARY = _guid(
    0x6245D6AF,
    0x66E0,
    0x48FD,
    (0x80, 0xB4, 0x4D, 0x27, 0x17, 0x96, 0x74, 0x8C),
)
IID_IDXC_LIBRARY = _guid(
    0xE5204DC7,
    0xD18C,
    0x4C3C,
    (0xBD, 0xFB, 0x85, 0x16, 0x73, 0x98, 0x0F, 0xE7),
)
CLSID_DXC_ASSEMBLER = _guid(
    0xD728DB68,
    0xF903,
    0x4F80,
    (0x94, 0xCD, 0xDC, 0xCF, 0x76, 0xEC, 0x71, 0x51),
)
IID_IDXC_ASSEMBLER = _guid(
    0x091F7A26,
    0x1C1F,
    0x4948,
    (0x90, 0x4B, 0xE6, 0xE3, 0xA8, 0xA7, 0x71, 0xD5),
)

# IUnknown occupies slots 0..2. These exact indexes follow the official
# legacy-interface declarations in dxcapi.h.
_RELEASE_SLOT = 2
_BLOB_GET_POINTER_SLOT = 3
_BLOB_GET_SIZE_SLOT = 4
_LIBRARY_CREATE_HEAP_COPY_SLOT = 7
_ASSEMBLER_ASSEMBLE_SLOT = 3
_OPERATION_GET_STATUS_SLOT = 3
_OPERATION_GET_RESULT_SLOT = 4
_OPERATION_GET_ERRORS_SLOT = 5


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


def _validate_ir(source: bytes) -> None:
    if not 1 <= len(source) <= MAX_IR_BYTES:
        raise RuntimeError("DXIL IR input must contain 1..%d bytes" % MAX_IR_BYTES)
    if b"\x00" in source:
        raise RuntimeError("DXIL IR input must be textual and contain no NUL bytes")
    try:
        source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("DXIL IR input must be strict UTF-8 text") from exc
    if _DXIL_TRIPLE_RE.search(source) is None:
        raise RuntimeError("DXIL IR input is missing an explicit dxil target triple")


def _validate_dxil_container(payload: bytes) -> None:
    if len(payload) < 36 or len(payload) > MAX_CONTAINER_BYTES:
        raise RuntimeError("IDxcAssembler returned an invalid DXIL container size")
    if payload[:4] != b"DXBC":
        raise RuntimeError("IDxcAssembler output is not a DXIL container")
    declared_size, part_count = struct.unpack_from("<II", payload, 24)
    if declared_size != len(payload):
        raise RuntimeError("IDxcAssembler output container size field is inconsistent")
    if not 1 <= part_count <= MAX_CONTAINER_PARTS:
        raise RuntimeError("IDxcAssembler output has an invalid part count")
    table_end = 32 + 4 * part_count
    if table_end > len(payload):
        raise RuntimeError("IDxcAssembler output part table is truncated")
    has_dxil = False
    seen: set[int] = set()
    for index in range(part_count):
        offset = struct.unpack_from("<I", payload, 32 + 4 * index)[0]
        if offset in seen or offset < table_end or offset + 8 > len(payload):
            raise RuntimeError("IDxcAssembler output contains an invalid part offset")
        seen.add(offset)
        fourcc = payload[offset : offset + 4]
        part_size = struct.unpack_from("<I", payload, offset + 4)[0]
        if offset + 8 + part_size > len(payload):
            raise RuntimeError("IDxcAssembler output contains a truncated part")
        has_dxil = has_dxil or fourcc == b"DXIL"
    if not has_dxil:
        raise RuntimeError("IDxcAssembler output has no DXIL program part")


def _method(
    instance: _COM,
    slot: int,
    result_type: type[ctypes._SimpleCData],
    *argument_types: type[ctypes._SimpleCData],
):
    if not instance or not instance.value:
        raise RuntimeError("DXC returned a null COM interface")
    object_words = ctypes.cast(instance, ctypes.POINTER(ctypes.c_void_p))
    vtable_address = object_words[0]
    if not vtable_address:
        raise RuntimeError("DXC returned a COM interface without a vtable")
    vtable = ctypes.cast(vtable_address, ctypes.POINTER(ctypes.c_void_p))
    method_address = vtable[slot]
    if not method_address:
        raise RuntimeError("DXC returned a COM interface with a missing method")
    return _STDCALL(result_type, ctypes.c_void_p, *argument_types)(method_address)


def _failed(hresult: int) -> bool:
    return int(hresult) < 0


def _require_success(label: str, hresult: int) -> None:
    if _failed(hresult):
        raise RuntimeError(
            "%s failed with HRESULT 0x%08X" % (label, int(hresult) & 0xFFFFFFFF)
        )


def _release(instance: _COM) -> None:
    if not instance or not instance.value:
        return
    release = _method(instance, _RELEASE_SLOT, _ULONG)
    release(instance)


def _blob_bytes(blob: _COM) -> bytes:
    if not blob or not blob.value:
        return b""
    get_pointer = _method(blob, _BLOB_GET_POINTER_SLOT, ctypes.c_void_p)
    get_size = _method(blob, _BLOB_GET_SIZE_SLOT, ctypes.c_size_t)
    size = int(get_size(blob))
    if size > MAX_CONTAINER_BYTES:
        raise RuntimeError("DXC returned an unexpectedly large blob")
    pointer = get_pointer(blob)
    if size and not pointer:
        raise RuntimeError("DXC returned a null buffer with non-zero size")
    return ctypes.string_at(pointer, size) if size else b""


def _assemble_bytes(dll_path: Path, source: bytes) -> tuple[int, bytes, bytes]:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("DXIL assembler adapter is supported only on Windows")

    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32. The
    # primary DLL is already absolute and hash-checked; dependencies cannot be
    # resolved from CWD or PATH.
    native = ctypes.WinDLL(str(dll_path), winmode=0x00000900)
    create_instance = native.DxcCreateInstance
    create_instance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.POINTER(_GUID),
        ctypes.POINTER(_COM),
    ]
    create_instance.restype = _HRESULT

    library = _COM()
    assembler = _COM()
    source_blob = _COM()
    operation = _COM()
    result_blob = _COM()
    error_blob = _COM()
    source_buffer = ctypes.create_string_buffer(source, len(source) + 1)
    try:
        _require_success(
            "DxcCreateInstance(CLSID_DxcLibrary)",
            int(
                create_instance(
                    ctypes.byref(CLSID_DXC_LIBRARY),
                    ctypes.byref(IID_IDXC_LIBRARY),
                    ctypes.byref(library),
                )
            ),
        )
        _require_success(
            "DxcCreateInstance(CLSID_DxcAssembler)",
            int(
                create_instance(
                    ctypes.byref(CLSID_DXC_ASSEMBLER),
                    ctypes.byref(IID_IDXC_ASSEMBLER),
                    ctypes.byref(assembler),
                )
            ),
        )
        create_blob = _method(
            library,
            _LIBRARY_CREATE_HEAP_COPY_SLOT,
            _HRESULT,
            ctypes.c_void_p,
            _UINT32,
            _UINT32,
            ctypes.POINTER(_COM),
        )
        _require_success(
            "IDxcLibrary::CreateBlobWithEncodingOnHeapCopy",
            int(
                create_blob(
                    library,
                    ctypes.cast(source_buffer, ctypes.c_void_p),
                    len(source),
                    65001,  # DXC_CP_UTF8
                    ctypes.byref(source_blob),
                )
            ),
        )
        assemble = _method(
            assembler,
            _ASSEMBLER_ASSEMBLE_SLOT,
            _HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(_COM),
        )
        _require_success(
            "IDxcAssembler::AssembleToContainer",
            int(assemble(assembler, source_blob, ctypes.byref(operation))),
        )

        get_status = _method(
            operation,
            _OPERATION_GET_STATUS_SLOT,
            _HRESULT,
            ctypes.POINTER(_HRESULT),
        )
        status = _HRESULT(0)
        _require_success(
            "IDxcOperationResult::GetStatus",
            int(get_status(operation, ctypes.byref(status))),
        )

        get_errors = _method(
            operation,
            _OPERATION_GET_ERRORS_SLOT,
            _HRESULT,
            ctypes.POINTER(_COM),
        )
        _require_success(
            "IDxcOperationResult::GetErrorBuffer",
            int(get_errors(operation, ctypes.byref(error_blob))),
        )
        diagnostics = _blob_bytes(error_blob)

        result = b""
        if not _failed(status.value):
            get_result = _method(
                operation,
                _OPERATION_GET_RESULT_SLOT,
                _HRESULT,
                ctypes.POINTER(_COM),
            )
            _require_success(
                "IDxcOperationResult::GetResult",
                int(get_result(operation, ctypes.byref(result_blob))),
            )
            result = _blob_bytes(result_blob)
        return int(status.value), result, diagnostics
    finally:
        for instance in (
            result_blob,
            error_blob,
            operation,
            source_blob,
            assembler,
            library,
        ):
            _release(instance)


def _write_native_diagnostics(payload: bytes) -> None:
    if not payload:
        return
    visible = payload[:-1] if payload.endswith(b"\x00") else payload
    stream = getattr(sys.stderr, "buffer", None)
    if stream is not None:
        stream.write(visible)
        if visible and not visible.endswith((b"\n", b"\r")):
            stream.write(b"\n")
        stream.flush()
    else:  # pragma: no cover - normal CPython stderr exposes buffer
        print(visible.decode("utf-8", errors="replace"), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", required=True)
    parser.add_argument("--dll-sha256", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parsed = parser.parse_args(argv)

    if os.name != "nt":
        raise RuntimeError("DXIL assembler adapter is supported only on Windows")
    dll = _absolute_regular_file(parsed.dll, "dxcompiler.dll")
    if dll.name.casefold() != _DXCOMPILER_FILE_NAME:
        raise RuntimeError("compiler DLL must be named dxcompiler.dll")
    expected_hash = str(parsed.dll_sha256).strip().casefold()
    if not _HASH_RE.fullmatch(expected_hash):
        raise RuntimeError("DLL SHA-256 must contain exactly 64 lowercase hex digits")
    if _sha256(dll) != expected_hash:
        raise RuntimeError("dxcompiler.dll SHA-256 changed before adapter load")

    source_path = _absolute_regular_file(parsed.input, "input")
    source = source_path.read_bytes()
    _validate_ir(source)
    output = _new_output_path(parsed.output)
    hresult, container, diagnostics = _assemble_bytes(dll, source)
    _write_native_diagnostics(diagnostics)
    if _failed(hresult):
        print(
            "IDxcAssembler failed with HRESULT 0x%08X" % (hresult & 0xFFFFFFFF),
            file=sys.stderr,
        )
        return 1
    _validate_dxil_container(container)
    with output.open("xb") as stream:
        stream.write(container)
        stream.flush()
        os.fsync(stream.fileno())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
