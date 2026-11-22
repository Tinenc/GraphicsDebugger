from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from src.symbols.recovery_assets import (
    RECOVERY_ASSET_SCHEMA,
    discover_recovery_assets,
    evaluate_recovery_candidate,
    read_recovery_regular_file,
)


def _make_dxbc(code: bytes = b"compiled instructions") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(program)) + program
    offset = 36
    total_size = offset + len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", total_size)
        + struct.pack("<I", 1)
        + struct.pack("<I", offset)
        + chunk
    )


def _make_dxil_library() -> bytes:
    bitcode = b"BC\xc0\xdefixed-library-bitcode"
    unpadded_size = 24 + len(bitcode)
    padded_size = (unpadded_size + 3) & ~3
    program = (
        struct.pack(
            "<6I",
            0x00060063,  # ProgramType Library, Shader Model 6.3.
            padded_size // 4,
            0x4C495844,
            0x00000100,
            16,
            len(bitcode),
        )
        + bitcode
        + b"\x00" * (padded_size - unpadded_size)
    )
    chunk = b"DXIL" + struct.pack("<I", len(program)) + program
    offset = 36
    total_size = offset + len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", total_size)
        + struct.pack("<I", 1)
        + struct.pack("<I", offset)
        + chunk
    )


def _spirv_string_words(value: str) -> list[int]:
    encoded = value.encode("utf-8") + b"\x00"
    encoded += b"\x00" * (-len(encoded) % 4)
    return list(struct.unpack(f"<{len(encoded) // 4}I", encoded))


def _make_spirv(entry: str = "main", execution_model: int = 4) -> bytes:
    name_words = _spirv_string_words(entry)
    op_entry_point = [
        ((3 + len(name_words)) << 16) | 15,
        execution_model,
        1,
        *name_words,
    ]
    words = [0x07230203, 0x00010600, 0, 2, 0, *op_entry_point]
    return struct.pack(f"<{len(words)}I", *words)


def _by_path(report: dict, relative_path: str) -> list[dict]:
    return [
        item for item in report["candidates"] if item["relativePath"] == relative_path
    ]


def test_discovers_common_sources_and_validated_binaries_stably(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "main.hlsl").write_text(
        "float4 MainPS() : SV_Target { return 1; }\n", encoding="utf-8"
    )
    (tmp_path / "nested" / "common.hlsli").write_text(
        "float4 Tint(float4 x) { return x; }\n", encoding="utf-8"
    )
    (tmp_path / "effect.vert").write_text(
        "#version 450\nvoid main() {}\n", encoding="utf-8"
    )
    (tmp_path / "lighting.wgsl").write_text(
        "@fragment fn shade() -> @location(0) vec4f { return vec4f(1); }\n",
        encoding="utf-8",
    )
    (tmp_path / "material.slang").write_text(
        '[shader("fragment")] float4 shade() { return 1; }\n', encoding="utf-8"
    )
    (tmp_path / "post.metal").write_text(
        "fragment float4 post() { return float4(1); }\n", encoding="utf-8"
    )
    unreal = tmp_path / "Engine" / "Shaders"
    unreal.mkdir(parents=True)
    (unreal / "Tonemap.usf").write_text(
        "float4 TonemapPS() : SV_Target { return 1; }\n", encoding="utf-8"
    )
    unity = tmp_path / "Assets" / "Shaders"
    unity.mkdir(parents=True)
    (unity / "Surface.shader").write_text(
        'Shader "Test" { SubShader { HLSLPROGRAM\n#pragma fragment Frag\nENDHLSL } }',
        encoding="utf-8",
    )

    dxbc = _make_dxbc()
    (tmp_path / "shader-cache.bin").write_bytes(b"prefix" + dxbc + b"suffix")
    dxil_library = _make_dxil_library()
    (tmp_path / "ray-library.dxil").write_bytes(dxil_library)
    spirv = _make_spirv()
    (tmp_path / "main.spv").write_bytes(spirv)
    (tmp_path / "default.metallib").write_bytes(b"MTLB" + b"\x00" * 28)

    first = discover_recovery_assets(tmp_path)
    second = discover_recovery_assets(tmp_path)

    assert first == second
    assert first["schema"] == RECOVERY_ASSET_SCHEMA
    assert first["schemaVersion"] == 1
    assert first["status"] == "complete"
    assert len(first["inventorySha256"]) == 64
    assert first["policy"] == {
        "assetCodeExecuted": False,
        "compilerInvoked": False,
        "allSourceAndEngineHintsTrusted": False,
        "applyAuthorized": False,
        "workspaceSeedAuthorized": False,
    }
    assert all(not item["trusted"] for item in first["candidates"])
    assert all(not item["applyAuthorized"] for item in first["candidates"])

    hlsl = _by_path(first, "nested/main.hlsl")[0]
    assert (hlsl["language"], hlsl["role"]) == ("hlsl", "source")
    assert (
        hlsl["fileSha256"]
        == hashlib.sha256((tmp_path / "nested" / "main.hlsl").read_bytes()).hexdigest()
    )
    assert _by_path(first, "nested/common.hlsli")[0]["role"] == "include"
    assert _by_path(first, "effect.vert")[0]["language"] == "glsl"
    assert _by_path(first, "lighting.wgsl")[0]["language"] == "wgsl"
    assert _by_path(first, "material.slang")[0]["language"] == "slang"
    assert _by_path(first, "post.metal")[0]["language"] == "msl"

    unreal_candidate = _by_path(first, "Engine/Shaders/Tonemap.usf")[0]
    assert "unreal-engine" in unreal_candidate["engineHints"]
    assert all(not hint["trusted"] for hint in unreal_candidate["engineHintEvidence"])
    unity_candidate = _by_path(first, "Assets/Shaders/Surface.shader")[0]
    assert "unity" in unity_candidate["engineHints"]

    d3d = _by_path(first, "shader-cache.bin")[0]
    assert d3d["format"] == "dxbc"
    assert d3d["fileOffset"] == len(b"prefix")
    assert d3d["binaryMetadata"]["embedded"]
    assert d3d["shaderFacts"]["stage"] == "fragment"
    assert d3d["shaderFacts"]["profile"] == "ps_5_0"
    assert d3d["fingerprints"]["rawSha256"] == hashlib.sha256(dxbc).hexdigest()

    library = _by_path(first, "ray-library.dxil")[0]
    assert library["format"] == "dxil"
    assert library["shaderFacts"]["stage"] == "library"
    assert library["shaderFacts"]["profile"] == "lib_6_3"

    spv = _by_path(first, "main.spv")[0]
    assert spv["format"] == "spir-v"
    assert spv["shaderFacts"]["stage"] == "fragment"
    assert spv["shaderFacts"]["entryPoint"] == "main"
    assert spv["fingerprints"]["rawSha256"] == hashlib.sha256(spirv).hexdigest()
    metal = _by_path(first, "default.metallib")[0]
    assert metal["format"] == "metallib"
    assert metal["binaryMetadata"]["headerOnlyRecognition"]


def test_inventory_hash_is_relocatable_capture_independent_and_content_bound(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    for root in (first_root, second_root):
        (root / "main.hlsl").write_text(
            "float4 MainPS() : SV_Target { return 1; }\n", encoding="utf-8"
        )
        # Fully read non-candidates are part of the inventory too.
        (root / "notes.txt").write_text("build notes", encoding="utf-8")

    first = discover_recovery_assets(
        first_root, {"stage": "fragment", "entry": "MainPS"}
    )
    relocated = discover_recovery_assets(
        second_root, {"stage": "vertex", "entry": "DifferentEntry"}
    )
    assert first["inventorySha256"] == relocated["inventorySha256"]

    (second_root / "notes.txt").write_text("changed build notes", encoding="utf-8")
    changed = discover_recovery_assets(second_root)
    assert changed["inventorySha256"] != first["inventorySha256"]


def test_capture_facts_rank_source_without_claiming_compile_or_apply(
    tmp_path: Path,
) -> None:
    (tmp_path / "HeroPS.hlsl").write_text(
        "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target { return 1; }\n",
        encoding="utf-8",
    )
    report = discover_recovery_assets(
        tmp_path,
        {
            "stage": "pixel",
            "targetProfile": {"profile": "ps_6_6"},
            "entryPoint": "MainPS",
            "displayName": "HeroPS",
        },
    )
    candidate = report["candidates"][0]

    assert candidate["sourceValue"] == 2
    assert candidate["compileUtility"] == 3
    assert candidate["recoveryLevel"] == 3
    assert candidate["identityValue"] == 1  # filename overlap is deliberately low
    assert candidate["matches"]["pathOrNameHint"]
    assert not candidate["applyAuthorized"]
    assert not candidate["workspaceSeedAuthorized"]
    assert not report["policy"]["compilerInvoked"]


def test_include_dependency_and_incompatible_language_cap_compile_utility(
    tmp_path: Path,
) -> None:
    (tmp_path / "with_include.hlsl").write_text(
        '#include "common.hlsli"\nfloat4 MainPS() : SV_Target { return 1; }\n',
        encoding="utf-8",
    )
    (tmp_path / "post.frag").write_text("void main() {}\n", encoding="utf-8")
    facts = {"stage": "fragment", "profile": "ps_6_6", "entry": "MainPS"}
    report = discover_recovery_assets(tmp_path, facts)
    hlsl = _by_path(report, "with_include.hlsl")[0]
    glsl = _by_path(report, "post.frag")[0]

    assert hlsl["sourceMetadata"]["includeDirectiveCount"] == 1
    assert hlsl["compileUtility"] == 2
    assert hlsl["recoveryLevel"] == 2
    assert glsl["compileUtility"] == 1
    assert glsl["recoveryLevel"] == 2  # still useful readable source, not D3D-ready


def test_recovery_levels_separate_raw_code_and_abi_evidence() -> None:
    raw = "a" * 64
    code = "b" * 64
    interface = "c" * 64
    layout = "d" * 64
    candidate = {
        "relativePath": "cache/Hero.bin",
        "displayName": "Hero",
        "role": "binary",
        "language": "dxil",
        "fingerprints": {"rawSha256": raw, "codeSha256": code},
        "shaderFacts": {
            "stage": "fragment",
            "profile": "ps_6_6",
            "entryPoint": "MainPS",
            "interfaceHash": interface,
            "layoutHash": layout,
        },
        # These caller claims must never confer authority.
        "trusted": True,
        "applyAuthorized": True,
    }
    common = {
        "stage": "pixel",
        "profile": "ps_6_6",
        "entry": "MainPS",
        "interfaceHash": interface,
        "layoutHash": layout,
    }

    raw_result = evaluate_recovery_candidate(candidate, {**common, "rawSha256": raw})
    assert (raw_result["identityValue"], raw_result["recoveryLevel"]) == (6, 6)
    assert not raw_result["applyAuthorized"]

    code_result = evaluate_recovery_candidate(
        candidate, {**common, "rawSha256": "e" * 64, "codeSha256": code}
    )
    assert (code_result["identityValue"], code_result["recoveryLevel"]) == (5, 5)

    abi_result = evaluate_recovery_candidate(
        candidate,
        {**common, "rawSha256": "e" * 64, "codeSha256": "f" * 64},
    )
    assert abi_result["identityValue"] == 3
    assert abi_result["abiFactsEqual"]
    assert abi_result["recoveryLevel"] == 4
    assert "semantic" in abi_result["identityReason"]


def test_path_or_name_alone_never_exceeds_level_one() -> None:
    candidate = {
        "relativePath": "Game/Shaders/HeroSkin.bin",
        "displayName": "HeroSkin",
        "role": "binary",
        "language": "unknown",
        "fingerprints": {},
        "shaderFacts": {},
    }
    result = evaluate_recovery_candidate(
        candidate, {"name": "HeroSkin", "path": "Captured/HeroSkin"}
    )
    assert result["matches"]["pathOrNameHint"]
    assert result["identityValue"] == 1
    assert result["recoveryLevel"] == 1
    assert not result["applyAuthorized"]


def test_structural_conflict_blocks_high_level_even_when_raw_hash_matches() -> None:
    digest = "a" * 64
    candidate = {
        "relativePath": "shader.bin",
        "role": "binary",
        "language": "dxbc",
        "fingerprints": {"rawSha256": digest},
        "shaderFacts": {"stage": "vertex", "profile": "vs_5_0"},
    }
    result = evaluate_recovery_candidate(
        candidate,
        {"rawSha256": digest, "stage": "fragment", "profile": "ps_5_0"},
    )
    assert result["identityValue"] == 6
    assert result["recoveryLevel"] <= 2
    assert len(result["conflicts"]) == 2
    assert not result["applyAuthorized"]


def test_file_count_size_and_total_read_limits_are_hard_bounds(tmp_path: Path) -> None:
    (tmp_path / "a.hlsl").write_bytes(b"a" * 8)
    (tmp_path / "b.hlsl").write_bytes(b"b" * 16)
    (tmp_path / "c.hlsl").write_bytes(b"c" * 32)

    file_limited = discover_recovery_assets(tmp_path, max_files=1)
    assert file_limited["status"] == "partial"
    assert file_limited["filesVisited"] == 1
    assert file_limited["candidateCount"] == 1
    assert "max-files-reached" in file_limited["partialReasons"]

    size_limited = discover_recovery_assets(tmp_path, max_file_size_bytes=12)
    assert size_limited["totalBytesRead"] == 8
    assert size_limited["skipped"]["tooLarge"] == 2
    assert "max-file-size-exceeded" in size_limited["partialReasons"]

    total_limited = discover_recovery_assets(tmp_path, max_total_read_bytes=20)
    assert total_limited["totalBytesRead"] <= 20
    assert total_limited["totalBytesRead"] == 8
    assert total_limited["skipped"]["totalBudget"] == 2
    assert "max-total-read-bytes-reached" in total_limited["partialReasons"]


def test_candidate_limit_bounds_embedded_container_expansion(tmp_path: Path) -> None:
    blob = _make_dxbc()
    (tmp_path / "many.cache").write_bytes(blob + blob + blob)
    report = discover_recovery_assets(tmp_path, max_candidates=1)

    assert report["candidateCount"] == 1
    assert report["candidateCountDiscovered"] == 3
    assert report["status"] == "partial"
    assert "max-candidates-reached" in report["partialReasons"]


def test_candidate_limit_is_ranked_top_k_and_late_exact_hash_wins(
    tmp_path: Path,
) -> None:
    (tmp_path / "a-first.hlsl").write_text(
        "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
    )
    exact = _make_dxbc(b"late exact executable")
    (tmp_path / "z-late.cache").write_bytes(exact)

    report = discover_recovery_assets(
        tmp_path,
        {
            "rawSha256": hashlib.sha256(exact).hexdigest(),
            # Deliberately conflict with the compiled token. Exact physical
            # identity must remain in top-K, but still cannot authorise apply.
            "stage": "vertex",
            "entryPoint": "main",
        },
        max_candidates=1,
    )

    assert report["filesVisited"] == 2
    assert report["candidateCountDiscovered"] == 2
    assert report["candidateCount"] == 1
    assert report["candidates"][0]["relativePath"] == "z-late.cache"
    assert report["candidates"][0]["identityValue"] == 6
    assert report["candidates"][0]["recoveryLevel"] <= 2
    assert report["candidates"][0]["matches"]["rawSha256Equal"]
    assert not report["candidates"][0]["applyAuthorized"]


def test_hint_fields_and_total_report_serialization_are_hard_bounded(
    tmp_path: Path,
) -> None:
    include_path = "x" * 500
    (tmp_path / "hints.hlsl").write_text(
        "".join('#include "%s"\n' % include_path for _ in range(1_000))
        + "float4 main() : SV_Target { return 1; }\n",
        encoding="utf-8",
    )
    for index in range(1_030):
        (tmp_path / ("shader-%04d.hlsl" % index)).write_text(
            "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
        )

    report = discover_recovery_assets(tmp_path)
    hints = _by_path(report, "hints.hlsl")[0]["sourceMetadata"]

    assert hints["includeDirectiveCount"] == 1_000
    assert len(hints["includeDirectives"]) <= report["limits"][
        "maximumHintItemsPerField"
    ]
    assert report["candidateCountDiscovered"] == 1_031
    assert report["candidateCount"] <= report["limits"]["maximumReportCandidates"]
    assert "max-report-candidates-reached" in report["partialReasons"]
    encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= report["limits"]["maximumReportJsonUtf8Bytes"]


def test_symlinks_are_skipped_and_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "inside.hlsl").write_text("float4 main() { return 1; }", encoding="utf-8")
    outside = tmp_path / "outside.hlsl"
    outside.write_text("float4 stolen() { return 0; }", encoding="utf-8")
    link = root / "outside-link.hlsl"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    report = discover_recovery_assets(root)
    assert report["skipped"]["symlinks"] == 1
    assert {item["relativePath"] for item in report["candidates"]} == {"inside.hlsl"}
    assert hashlib.sha256(outside.read_bytes()).hexdigest() not in {
        item["fileSha256"] for item in report["candidates"]
    }

    root_link = tmp_path / "root-link"
    try:
        os.symlink(root, root_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ValueError, match="must not be a symlink"):
        discover_recovery_assets(root_link)


def test_handle_bound_reader_rejects_parent_reparse_without_path_trust(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    target = root / "target"
    nested.mkdir(parents=True)
    target.mkdir()
    payload = b"fixed shader bytes"
    (nested / "shader.bin").write_bytes(payload)
    (target / "shader.bin").write_bytes(payload)

    assert read_recovery_regular_file(
        root, "nested/shader.bin", len(payload)
    ) == (payload, "", len(payload))
    with pytest.raises(ValueError, match="remain below"):
        read_recovery_regular_file(root, "../target/shader.bin", len(payload))

    redirected = root / "redirected"
    root_reparse = tmp_path / "root-reparse"
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(redirected))
            _winapi.CreateJunction(str(root), str(root_reparse))
        except (ImportError, OSError):
            pytest.skip("Windows junction creation is unavailable")
        # Junctions are reparse points even though pathlib does not classify
        # them as symlinks. The handle open must still reject them.
        assert not redirected.is_symlink()
    else:
        try:
            os.symlink(target, redirected, target_is_directory=True)
            os.symlink(root, root_reparse, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable")

    data, reason, consumed = read_recovery_regular_file(
        root, "redirected/shader.bin", len(payload)
    )
    assert data is None
    assert reason in {"reparse-or-symlink", "reparse-or-path-race"}
    assert consumed == 0
    with pytest.raises(ValueError, match="symlink or reparse point"):
        discover_recovery_assets(root_reparse)


def test_malformed_binary_and_asset_script_are_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    (tmp_path / "asset.py").write_text(
        f"from pathlib import Path\nPath({json.dumps(str(marker))}).write_text('bad')\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.spv").write_bytes(b"\x03\x02\x23\x07" + b"\x00" * 16)
    (tmp_path / "broken.dxil").write_bytes(b"DXBC" + b"\x00" * 64)

    report = discover_recovery_assets(tmp_path)
    assert not marker.exists()
    assert report["candidateCount"] == 0
    assert not report["policy"]["assetCodeExecuted"]
    assert not report["policy"]["compilerInvoked"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_files": 0},
        {"max_file_size_bytes": -1},
        {"max_total_read_bytes": False},
        {"max_candidates": "not-an-int"},
    ],
)
def test_invalid_limits_fail_closed(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises((TypeError, ValueError), match="positive integer"):
        discover_recovery_assets(tmp_path, **kwargs)


def test_invalid_capture_hash_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        discover_recovery_assets(tmp_path, {"rawSha256": "not-a-digest"})
