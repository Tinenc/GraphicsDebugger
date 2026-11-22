from __future__ import annotations

import unittest
import subprocess
import tempfile
from pathlib import Path

from src.shader_workspace.adapters.dxil_access_signature import (
    DxilCBufferAccess,
    DxilCBufferAccessBlock,
    DxilCBufferAccessSignature,
    extract_dxil_cbuffer_access_signature,
)
from src.shader_workspace.adapters.hlsl_cbuffer_abi_repair import (
    HlslCBufferAbiRepairError,
    repair_spirv_cross_hlsl_cbuffers,
)


def block(
    bind: int,
    byte_size: int,
    accesses: list[tuple[int, int, str]],
    *,
    space: int = 0,
    bind_array_size: int = 1,
) -> DxilCBufferAccessBlock:
    values = tuple(
        DxilCBufferAccess(offset, width, return_type)
        for offset, width, return_type in accesses
    )
    return DxilCBufferAccessBlock(
        space,
        bind,
        bind_array_size,
        byte_size,
        min(item.byte_offset for item in values),
        max(item.byte_end for item in values),
        values,
    )


def signature(*blocks: DxilCBufferAccessBlock) -> DxilCBufferAccessSignature:
    return DxilCBufferAccessSignature("0" * 64, 1, tuple(blocks))


def baseline(
    bind: int,
    byte_size: int,
    *,
    completeness: str = "ambiguous",
    space: int = 0,
    bind_array_size: int = 1,
) -> dict[str, object]:
    return {
        "space": space,
        "bind": bind,
        "bindArraySize": bind_array_size,
        "byteSize": byte_size,
        "layoutCompleteness": completeness,
    }


CURRENT_SHAPE = """cbuffer B0 : register(b0, space0)
{
    float4 B0Raw[1] : packoffset(c0);
};

cbuffer B1 : register(b1, space0)
{
    float4 B1Raw[474] : packoffset(c0);
};

cbuffer B2 : register(b2, space0)
{
    float4 B2Raw[33] : packoffset(c0);
};

cbuffer B3 : register(b3, space0)
{
    float4 B3Raw[54] : packoffset(c0);
};

float4 MainPS() : SV_Target0
{
    uint globalBits = asuint(B0Raw[0u]).x;
    float viewA = B1Raw[44u].w;
    uint viewFlag = asuint(B1Raw[154u]).z;
    float viewB = B1Raw[205u].w;
    float collection = B2Raw[8u].w;
    float material = B3Raw[14u].w;
    return float4(viewA + viewB + collection + material, globalBits, viewFlag, 1.0f);
}
"""


class HlslCBufferAbiRepairTests(unittest.TestCase):
    def test_dynamic_umin_add_repair_round_trips_through_real_dxc(self) -> None:
        dxc = Path(__file__).parents[3] / "x64/Development/replaylab/tools/dxc.exe"
        if not dxc.is_file():
            self.skipTest("bundled DXC is unavailable")
        baseline_source = """cbuffer B : register(b0, space0) {
float4 Values[8] : packoffset(c0); float Tail : packoffset(c8.x);
};
float4 MainPS(nointerpolation uint inputIndex : TEXCOORD0) : SV_Target0 {
return Values[min(inputIndex, 7u)] + Tail;
}
"""
        lowered = """cbuffer B : register(b0, space0) { float4 Raw[9] : packoffset(c0); };
float4 MainPS(nointerpolation uint inputIndex : TEXCOORD0) : SV_Target0 {
uint boundedIndex = min(inputIndex, 7u) + 0u;
return Raw[boundedIndex] + Raw[8u].x;
}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def compile_source(name: str, content: str):
                path = root / (name + ".hlsl")
                path.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [str(dxc), "-T", "ps_6_6", "-E", "MainPS", "-Fc", str(root / (name + ".ll")),
                     "-Fo", str(root / (name + ".dxil")), str(path)], capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                return extract_dxil_cbuffer_access_signature((root / (name + ".ll")).read_bytes())
            original_access = compile_source("original", baseline_source)
            repaired = repair_spirv_cross_hlsl_cbuffers(
                lowered, baseline_constant_blocks=[baseline(0, 132)], dxil_access=original_access,
            )
            candidate_access = compile_source("candidate", repaired.hlsl)
            self.assertEqual(original_access.signature_sha256, candidate_access.signature_sha256)
            self.assertIn("switch (index)", repaired.hlsl)
            self.assertEqual(candidate_access.block(0, 0).byte_size, 132)
            with self.assertRaisesRegex(HlslCBufferAbiRepairError, "provably uint"):
                repair_spirv_cross_hlsl_cbuffers(
                    lowered.replace("uint inputIndex", "float inputIndex"),
                    baseline_constant_blocks=[baseline(0, 132)], dxil_access=original_access,
                )

    def test_repairs_current_odd_tails_and_integer_lanes_only(self) -> None:
        result = repair_spirv_cross_hlsl_cbuffers(
            CURRENT_SHAPE,
            baseline_constant_blocks=[
                baseline(0, 8),
                baseline(1, 7572),
                baseline(2, 528, completeness="complete"),
                baseline(3, 860),
            ],
            dxil_access=signature(
                block(0, 8, [(0, 4, "i32")]),
                block(
                    1,
                    7572,
                    [
                        (716, 4, "f32"),
                        (2472, 4, "i32"),
                        (3292, 4, "f32"),
                    ],
                ),
                block(2, 528, [(140, 4, "f32")]),
                block(3, 860, [(236, 4, "f32")]),
            ),
        )
        self.assertEqual(
            [(0, 8, True), (1, 7572, True), (3, 860, True)],
            [
                (item.bind, item.byte_size, item.tail_guard_added)
                for item in result.repaired_blocks
            ],
        )
        self.assertIn(
            "uint __replaylab_s0_b0_c0_x_i32 : packoffset(c0.x);",
            result.hlsl,
        )
        self.assertIn(
            "uint __replaylab_s0_b0_c0_y_tail : packoffset(c0.y);",
            result.hlsl,
        )
        self.assertIn(
            "float __replaylab_s0_b1_c44_w_f32 : packoffset(c44.w);",
            result.hlsl,
        )
        self.assertIn(
            "uint __replaylab_s0_b1_c154_z_i32 : packoffset(c154.z);",
            result.hlsl,
        )
        self.assertIn(
            "uint __replaylab_s0_b1_c473_x_tail : packoffset(c473.x);",
            result.hlsl,
        )
        self.assertIn(
            "uint __replaylab_s0_b3_c53_z_tail : packoffset(c53.z);",
            result.hlsl,
        )
        self.assertIn(
            "float4 B2Raw[33] : packoffset(c0);",
            result.hlsl,
        )
        self.assertNotIn("B0Raw[", result.hlsl)
        self.assertNotIn("B1Raw[", result.hlsl)
        self.assertNotIn("B3Raw[", result.hlsl)
        self.assertIn(
            "asuint(float4(asfloat(__replaylab_s0_b0_c0_x_i32), "
            "0.0f, 0.0f, 0.0f)).x",
            result.hlsl,
        )
        self.assertIn(
            "asuint(float4(0.0f, 0.0f, "
            "asfloat(__replaylab_s0_b1_c154_z_i32), 0.0f)).z",
            result.hlsl,
        )

    def test_does_not_add_tail_when_last_access_ends_at_envelope(self) -> None:
        source = """cbuffer B : register(b4, space2)
{
    float4 Raw[1] : packoffset(c0.x);
};
float main() : SV_Target { return Raw[0].y; }
"""
        result = repair_spirv_cross_hlsl_cbuffers(
            source,
            baseline_constant_blocks=[baseline(4, 8, space=2)],
            dxil_access=signature(block(4, 8, [(4, 4, "f32")], space=2)),
        )
        self.assertFalse(result.repaired_blocks[0].tail_guard_added)
        self.assertIn("packoffset(c0.y);", result.hlsl)
        self.assertNotIn("_tail", result.hlsl)

    def test_rejects_non_simple_cbuffer_body(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
    float Extra : packoffset(c1.x);
};
float main() : SV_Target { return Raw[0].x; }
"""
        with self.assertRaisesRegex(
            HlslCBufferAbiRepairError, "not the simple SPIRV-Cross shape"
        ):
            repair_spirv_cross_hlsl_cbuffers(
                source,
                baseline_constant_blocks=[baseline(0, 8)],
                dxil_access=signature(block(0, 8, [(0, 4, "f32")])),
            )

    def test_rejects_wrong_rounded_array_count(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[2] : packoffset(c0);
};
float main() : SV_Target { return Raw[0].x; }
"""
        with self.assertRaisesRegex(
            HlslCBufferAbiRepairError, "rounded baseline size"
        ):
            repair_spirv_cross_hlsl_cbuffers(
                source,
                baseline_constant_blocks=[baseline(0, 8)],
                dxil_access=signature(block(0, 8, [(0, 4, "f32")])),
            )

    def test_rejects_dynamic_index(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
};
float main(uint i : TEXCOORD0) : SV_Target { return Raw[i].x; }
"""
        with self.assertRaisesRegex(
            HlslCBufferAbiRepairError, "dynamic or unsupported"
        ):
            repair_spirv_cross_hlsl_cbuffers(
                source,
                baseline_constant_blocks=[baseline(0, 8)],
                dxil_access=signature(block(0, 8, [(0, 4, "f32")])),
            )

    def test_rejects_lane_missing_from_access_evidence(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
};
float main() : SV_Target { return Raw[0].y; }
"""
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "lane absent"):
            repair_spirv_cross_hlsl_cbuffers(
                source,
                baseline_constant_blocks=[baseline(0, 8)],
                dxil_access=signature(block(0, 8, [(0, 4, "f32")])),
            )

    def test_rejects_unrepresented_access_evidence(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
};
float main() : SV_Target { return Raw[0].x; }
"""
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "complete DXIL"):
            repair_spirv_cross_hlsl_cbuffers(
                source,
                baseline_constant_blocks=[baseline(0, 8)],
                dxil_access=signature(
                    block(0, 8, [(0, 4, "f32"), (4, 4, "f32")])
                ),
            )

    def test_rejects_unsupported_or_aliased_physical_access(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
};
float main() : SV_Target { return Raw[0].x; }
"""
        for accesses, message in (
            ([(0, 2, "f16")], "aligned f32/i32"),
            ([(0, 4, "f32"), (0, 4, "i32")], "multiple physical"),
        ):
            with self.subTest(accesses=accesses), self.assertRaisesRegex(
                HlslCBufferAbiRepairError, message
            ):
                repair_spirv_cross_hlsl_cbuffers(
                    source,
                    baseline_constant_blocks=[baseline(0, 8)],
                    dxil_access=signature(block(0, 8, accesses)),
                )

    def test_rejects_missing_or_mismatched_access_block(self) -> None:
        source = """cbuffer B : register(b0, space0)
{
    float4 Raw[1] : packoffset(c0);
};
float main() : SV_Target { return Raw[0].x; }
"""
        for access, message in (
            (signature(block(1, 8, [(0, 4, "f32")])), "no DXIL"),
            (signature(block(0, 16, [(0, 4, "f32")])), "contradicts"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                HlslCBufferAbiRepairError, message
            ):
                repair_spirv_cross_hlsl_cbuffers(
                    source,
                    baseline_constant_blocks=[baseline(0, 8)],
                    dxil_access=access,
                )

    def test_rejects_without_ambiguous_baseline(self) -> None:
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "no ambiguous"):
            repair_spirv_cross_hlsl_cbuffers(
                CURRENT_SHAPE,
                baseline_constant_blocks=[
                    baseline(0, 8, completeness="complete")
                ],
                dxil_access=signature(block(0, 8, [(0, 4, "i32")])),
            )


if __name__ == "__main__":
    unittest.main()
