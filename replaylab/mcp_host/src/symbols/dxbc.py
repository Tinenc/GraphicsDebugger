"""Small, dependency-free DXBC/DXIL container scanner.

DXIL programs are also stored in a DXBC container.  This module does not try to
decompile shaders; it only finds well-formed containers and computes stable
fingerprints.  Keeping this deterministic makes it suitable as the first stage
before any AI-assisted matching.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mmap
from pathlib import Path
import struct
from typing import Iterable, Iterator, Sequence


DXBC_MAGIC = b"DXBC"
EXECUTABLE_CHUNKS = frozenset((b"SHDR", b"SHEX", b"DXIL"))
MAX_CHUNKS = 4096


@dataclass(frozen=True)
class Chunk:
    fourcc: str
    offset: int
    size: int


@dataclass(frozen=True)
class ShaderContainer:
    path: str
    file_offset: int
    byte_size: int
    kind: str
    raw_sha256: str
    code_sha256: str
    chunks: tuple[Chunk, ...]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "fileOffset": self.file_offset,
            "byteSize": self.byte_size,
            "kind": self.kind,
            "rawSha256": self.raw_sha256,
            "codeSha256": self.code_sha256,
            "chunks": [
                {"fourcc": c.fourcc, "offset": c.offset, "size": c.size}
                for c in self.chunks
            ],
        }


def _u32(data: memoryview, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _parse_container(data: memoryview, start: int, source: str) -> ShaderContainer | None:
    remaining = len(data) - start
    if remaining < 32 or bytes(data[start : start + 4]) != DXBC_MAGIC:
        return None

    try:
        total_size = _u32(data, start + 24)
        chunk_count = _u32(data, start + 28)
    except (IndexError, struct.error):
        return None

    header_size = 32 + chunk_count * 4
    if (
        total_size < header_size
        or total_size > remaining
        or chunk_count == 0
        or chunk_count > MAX_CHUNKS
    ):
        return None

    chunks: list[Chunk] = []
    executable_parts: list[bytes] = []
    has_dxil = False
    occupied: list[tuple[int, int]] = []

    for index in range(chunk_count):
        try:
            relative = _u32(data, start + 32 + index * 4)
        except (IndexError, struct.error):
            return None

        if relative < header_size or relative + 8 > total_size:
            return None

        chunk_start = start + relative
        fourcc_bytes = bytes(data[chunk_start : chunk_start + 4])
        try:
            chunk_size = _u32(data, chunk_start + 4)
        except (IndexError, struct.error):
            return None

        chunk_end_relative = relative + 8 + chunk_size
        if chunk_end_relative > total_size:
            return None

        # Duplicate/overlapping chunk ranges are malformed and are a common
        # source of false positives when scanning arbitrary packed assets.
        for used_start, used_end in occupied:
            if relative < used_end and chunk_end_relative > used_start:
                return None
        occupied.append((relative, chunk_end_relative))

        fourcc = fourcc_bytes.decode("ascii", errors="replace")
        chunks.append(Chunk(fourcc=fourcc, offset=relative, size=chunk_size))
        if fourcc_bytes in EXECUTABLE_CHUNKS:
            executable_parts.append(
                fourcc_bytes
                + struct.pack("<I", chunk_size)
                + bytes(data[chunk_start + 8 : chunk_start + 8 + chunk_size])
            )
        if fourcc_bytes == b"DXIL":
            has_dxil = True

    raw = bytes(data[start : start + total_size])
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    code_material = b"".join(executable_parts) or raw
    code_sha256 = hashlib.sha256(code_material).hexdigest()

    return ShaderContainer(
        path=source,
        file_offset=start,
        byte_size=total_size,
        kind="DXIL" if has_dxil else "DXBC",
        raw_sha256=raw_sha256,
        code_sha256=code_sha256,
        chunks=tuple(chunks),
    )


def fingerprint_bytes(data: bytes, source: str = "<memory>") -> ShaderContainer | None:
    """Fingerprint one buffer that is expected to begin with a DXBC container."""

    return _parse_container(memoryview(data), 0, source)


def scan_file(path: str | Path, max_containers: int = 10000) -> list[ShaderContainer]:
    """Find all valid DXBC/DXIL containers embedded in *path*.

    The file is memory-mapped so large asset archives are not copied into Python
    memory.  Invalid magic occurrences are skipped without aborting the scan.
    """

    shader_path = Path(path)
    if not shader_path.is_file() or shader_path.stat().st_size < 32:
        return []

    found: list[ShaderContainer] = []
    with shader_path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            view = memoryview(mapped)
            cursor = 0
            try:
                while len(found) < max_containers:
                    offset = mapped.find(DXBC_MAGIC, cursor)
                    if offset < 0:
                        break
                    parsed = _parse_container(view, offset, str(shader_path))
                    if parsed is not None:
                        found.append(parsed)
                        cursor = offset + parsed.byte_size
                    else:
                        cursor = offset + 4
            finally:
                view.release()
    return found


def read_container_bytes(container: ShaderContainer) -> bytes:
    with open(container.path, "rb") as stream:
        stream.seek(container.file_offset)
        return stream.read(container.byte_size)
