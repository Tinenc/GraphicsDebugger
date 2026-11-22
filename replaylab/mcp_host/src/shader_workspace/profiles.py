"""Read an exact compiler target profile from a validated executable header.

DXBC tokenized programs encode stage and shader-model version in the first
DWORD of SHDR/SHEX and their total DWORD count in the second. DXIL encodes the
same fields in ``DxilProgramHeader.ProgramVersion`` at the start of the DXIL
chunk. A token is accepted only after its complete program header agrees with
the containing part; trusting the first DWORD alone would turn malformed
container data into a compiler command-line target.

10Level9 DXBC containers carry an ``Aon9`` part while their SHDR/SHEX token
still resembles an ordinary ``*_4_0`` program. The public token does not
unambiguously distinguish the precise ``*_4_0_level_9_x`` target, so those
containers deliberately fail closed instead of returning a guessed profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct
from typing import Any

from ..symbols.dxbc import fingerprint_bytes
from .catalog import normalize_stage
from .errors import ToolchainRoutingError

_KIND_TO_STAGE_PREFIX = {
    0: ("fragment", "ps"),
    1: ("vertex", "vs"),
    2: ("geometry", "gs"),
    3: ("hull", "hs"),
    4: ("domain", "ds"),
    5: ("compute", "cs"),
}
_DXIL_EXTRA_KIND_TO_STAGE_PREFIX = {
    # DXIL libraries compile with a lib_* target.  A library container can
    # expose one or more ray-tracing entry points whose physical stages live in
    # dx.entryPoints/RDAT rather than in this container-level ProgramType.
    6: ("library", "lib"),
    7: ("raygen", "lib"),
    8: ("intersection", "lib"),
    9: ("anyhit", "lib"),
    10: ("closesthit", "lib"),
    11: ("miss", "lib"),
    12: ("callable", "lib"),
    13: ("mesh", "ms"),
    14: ("task", "as"),
}
_DXIL_LIBRARY_EXPORT_STAGES = frozenset(
    {"raygen", "intersection", "anyhit", "closesthit", "miss", "callable"}
)
_PREFIX_TO_STAGE = {
    prefix: stage
    for stage, prefix in tuple(_KIND_TO_STAGE_PREFIX.values())
    + tuple(_DXIL_EXTRA_KIND_TO_STAGE_PREFIX.values())
    if prefix != "lib"
}
_PROFILE_RE = re.compile(r"^(ps|vs|gs|hs|ds|cs|ms|as|lib)_(\d+)_(\d+)$")

# ``DxilProgramHeader`` is two uint32 values followed by a
# ``DxilBitcodeHeader`` containing four uint32 values. BitcodeOffset is relative
# to the start of the latter header (eight bytes into the program header).
_DXIL_PROGRAM_HEADER_SIZE = 24
_DXIL_BITCODE_HEADER_OFFSET = 8
_DXIL_BITCODE_HEADER_SIZE = 16
_DXIL_MAGIC = 0x4C495844  # little-endian ASCII "DXIL"
_DXBC_TOKENIZED_PROGRAM_HEADER_SIZE = 8


@dataclass(frozen=True)
class TargetProfile:
    profile: str
    container_format: str
    stage: str
    major: int
    minor: int
    executable_chunk: str
    token: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "containerFormat": self.container_format,
            "stage": self.stage,
            "shaderModelMajor": self.major,
            "shaderModelMinor": self.minor,
            "executableChunk": self.executable_chunk,
            "programVersionToken": "0x%08x" % self.token,
            "source": "compiled executable token",
            "guessed": False,
        }


def extract_target_profile(data: bytes, expected_stage: str | None = None) -> TargetProfile:
    container = fingerprint_bytes(data)
    if container is None or container.byte_size != len(data):
        raise ToolchainRoutingError("target profile requires exactly one valid DXBC/DXIL container")
    executable = [
        chunk for chunk in container.chunks if chunk.fourcc in {"SHDR", "SHEX", "DXIL"}
    ]
    if len(executable) != 1:
        raise ToolchainRoutingError(
            "target profile requires exactly one executable SHDR/SHEX/DXIL chunk"
        )
    chunk = executable[0]
    if any(part.fourcc == "Aon9" for part in container.chunks):
        raise ToolchainRoutingError(
            "10Level9/downlevel DXBC contains Aon9; the exact *_4_0_level_9_x "
            "target cannot be derived unambiguously and will not be guessed"
        )
    if chunk.fourcc == "DXIL":
        token = _validated_dxil_program_version(data, chunk.offset + 8, chunk.size)
    else:
        token = _validated_dxbc_program_version(data, chunk.offset + 8, chunk.size)
    kind = (token >> 16) & 0xFFFF
    major = (token >> 4) & 0xF
    minor = token & 0xF
    mapping = dict(_KIND_TO_STAGE_PREFIX)
    if chunk.fourcc == "DXIL":
        mapping.update(_DXIL_EXTRA_KIND_TO_STAGE_PREFIX)
        if major < 6:
            raise ToolchainRoutingError("DXIL program version does not declare Shader Model 6+")
    else:
        if major not in {4, 5} or (major == 4 and minor not in {0, 1}) or (
            major == 5 and minor not in {0, 1}
        ):
            raise ToolchainRoutingError(
                "unsupported or malformed DXBC shader model %d_%d" % (major, minor)
            )
    if kind not in mapping:
        raise ToolchainRoutingError("unsupported executable shader kind %d" % kind)
    stage, prefix = mapping[kind]
    if expected_stage is not None:
        normalized_expected_stage = normalize_stage(expected_stage)
        # A lib_* container declares ProgramType Library even when the selected
        # capture entry is a RayGen/Miss/Hit/Callable export.  The export stage
        # is independently bound by the capture shader key and fresh
        # reflection; it cannot be inferred from ProgramVersion.  Accept that
        # exact container/export relationship without relabelling the physical
        # ProgramType as the caller's requested stage.
        library_export = (
            stage == "library"
            and normalized_expected_stage in _DXIL_LIBRARY_EXPORT_STAGES
        )
        if normalized_expected_stage != stage and not library_export:
            raise ToolchainRoutingError(
                "compiled target stage %s contradicts expected stage %s"
                % (stage, normalized_expected_stage)
            )
    return TargetProfile(
        profile="%s_%d_%d" % (prefix, major, minor),
        container_format=container.kind.lower(),
        stage=stage,
        major=major,
        minor=minor,
        executable_chunk=chunk.fourcc,
        token=token,
    )


def _validated_dxbc_program_version(data: bytes, offset: int, chunk_size: int) -> int:
    """Return VersionToken for an exactly sized tokenized DXBC program.

    SHDR/SHEX begin with VersionToken and Length. Length is the total program
    size in DWORDs, including both header DWORDs. A standalone first token is
    therefore insufficient evidence for a compiler profile.
    """

    if chunk_size < _DXBC_TOKENIZED_PROGRAM_HEADER_SIZE or offset < 0:
        raise ToolchainRoutingError(
            "DXBC SHDR/SHEX chunk is too small for VersionToken and Length"
        )
    if chunk_size & 3:
        raise ToolchainRoutingError("DXBC SHDR/SHEX chunk size is not DWORD-aligned")
    if offset > len(data) or chunk_size > len(data) - offset:
        raise ToolchainRoutingError("DXBC tokenized program exceeds the container bounds")

    try:
        token, length_in_dwords = struct.unpack_from("<2I", data, offset)
    except struct.error as exc:
        raise ToolchainRoutingError("DXBC tokenized program header is truncated") from exc

    if length_in_dwords < (_DXBC_TOKENIZED_PROGRAM_HEADER_SIZE // 4):
        raise ToolchainRoutingError("DXBC tokenized program Length is smaller than its header")
    if length_in_dwords != chunk_size // 4:
        raise ToolchainRoutingError(
            "DXBC tokenized program Length does not exactly match the SHDR/SHEX chunk size"
        )
    if token & 0x0000FF00:
        raise ToolchainRoutingError("DXBC program version has non-zero reserved bits")

    return token


def _validated_dxil_program_version(data: bytes, offset: int, chunk_size: int) -> int:
    """Return ProgramVersion only for a self-consistent DXIL program part.

    The checks mirror the bounds guaranteed by DirectXShaderCompiler's
    ``IsValidDxilProgramHeader``/``IsValidDxilBitcodeHeader`` and additionally
    require the three independent size declarations (part size,
    ``SizeInUint32``, and padded bitcode end) to agree exactly. Exact agreement
    is important here because this parser is selecting a compiler target, not
    merely locating a possibly embedded program.
    """

    if chunk_size < _DXIL_PROGRAM_HEADER_SIZE or offset < 0:
        raise ToolchainRoutingError(
            "DXIL chunk is too small for DxilProgramHeader/DxilBitcodeHeader"
        )
    if offset > len(data) or chunk_size > len(data) - offset:
        raise ToolchainRoutingError("DXIL program header exceeds the container bounds")

    try:
        (
            program_version,
            size_in_uint32,
            dxil_magic,
            _dxil_version,
            bitcode_offset,
            bitcode_size,
        ) = struct.unpack_from("<6I", data, offset)
    except struct.error as exc:
        raise ToolchainRoutingError("DXIL program header is truncated") from exc

    if program_version & 0x0000FF00:
        raise ToolchainRoutingError("DXIL ProgramVersion has non-zero reserved bits")

    if size_in_uint32 < (_DXIL_PROGRAM_HEADER_SIZE // 4):
        raise ToolchainRoutingError("DXIL SizeInUint32 is smaller than the program header")
    declared_size = size_in_uint32 * 4
    if declared_size != chunk_size:
        raise ToolchainRoutingError(
            "DXIL SizeInUint32 does not exactly match the DXIL chunk size"
        )

    if dxil_magic != _DXIL_MAGIC:
        raise ToolchainRoutingError("DXIL bitcode header has invalid DXIL magic")
    if bitcode_offset < _DXIL_BITCODE_HEADER_SIZE:
        raise ToolchainRoutingError("DXIL BitcodeOffset overlaps the bitcode header")
    if bitcode_size == 0:
        raise ToolchainRoutingError("DXIL BitcodeSize must be non-zero")

    # Offsets in DxilBitcodeHeader are relative to that header, not to the
    # beginning of the part. Subtraction-based checks avoid 32-bit wraparound
    # ambiguities in hostile data.
    available_from_bitcode_header = declared_size - _DXIL_BITCODE_HEADER_OFFSET
    if bitcode_offset > available_from_bitcode_header:
        raise ToolchainRoutingError("DXIL BitcodeOffset exceeds the declared program size")
    if bitcode_size > available_from_bitcode_header - bitcode_offset:
        raise ToolchainRoutingError("DXIL bitcode range exceeds the declared program size")

    bitcode_end = _DXIL_BITCODE_HEADER_OFFSET + bitcode_offset + bitcode_size
    padded_bitcode_end = (bitcode_end + 3) & ~3
    if padded_bitcode_end != declared_size:
        raise ToolchainRoutingError(
            "DXIL bitcode range is inconsistent with SizeInUint32/chunk padding"
        )

    return program_version


def validate_target_profile(profile: str, expected_stage: str) -> str:
    value = str(profile or "").strip().lower()
    match = _PROFILE_RE.fullmatch(value)
    if match is None:
        raise ToolchainRoutingError("target profile must be an exact stage_major_minor profile")
    prefix = match.group(1)
    normalized_expected_stage = normalize_stage(expected_stage)
    if prefix == "lib":
        if normalized_expected_stage not in {"library", *_DXIL_LIBRARY_EXPORT_STAGES}:
            raise ToolchainRoutingError(
                "target profile stage library contradicts expected stage %s"
                % normalized_expected_stage
            )
        return value
    stage = _PREFIX_TO_STAGE[prefix]
    if stage != normalized_expected_stage:
        raise ToolchainRoutingError(
            "target profile stage %s contradicts expected stage %s"
            % (stage, normalized_expected_stage)
        )
    return value
