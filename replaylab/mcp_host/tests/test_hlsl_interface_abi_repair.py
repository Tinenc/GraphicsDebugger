from __future__ import annotations

import copy
import unittest

from src.shader_workspace.adapters.hlsl_cbuffer_abi_repair import HlslCBufferAbiRepairError
from src.shader_workspace.adapters.hlsl_interface_abi_repair import repair_spirv_cross_hlsl_interface


def interface() -> dict:
    def row(name, register, *, width=4, base="float", used=15, mask=15, system="undefined"):
        return {"semanticName": name, "semanticIdxName": name, "semanticIndex": 0,
                "regIndex": register, "regChannelMask": mask, "channelUsedMask": used,
                "compCount": width, "compType": base, "systemValue": system}
    def resource(bind, width, descriptor, base="float"):
        return {"space": 0, "bind": bind, "arraySize": 1, "descriptorType": descriptor,
                "variableType": {"baseType": base, "columns": width, "rows": 1,
                                 "elements": 1, "byteSize": width * 4, "members": []}}
    return {
        "inputSignature": [
            row("sv_position", 0, system="position"), row("color", 1),
            row("primitive_id", 2, width=1, base="uint", mask=1, used=1),
            row("sv_isfrontface", 2, width=1, base="uint", mask=2, used=0, system="isfrontface"),
        ],
        "readOnlyResources": [resource(0, 2, "buffer"), resource(1, 1, "typedbuffer")],
    }


LOWERED = """Buffer<uint4> Words : register(t0, space0);
Buffer<float4> Scalars : register(t1, space0);
struct SPIRV_Cross_Input {
float4 tint : TEXCOORD1;
nointerpolation uint prim : TEXCOORD2;
float4 position : SV_Position;
};
float4 MainPS(SPIRV_Cross_Input input) : SV_Target0 {
float word = asfloat(Words.Load(input.prim * 2u + 1u).x);
return input.tint * (word + Scalars.Load(input.prim).x);
}
"""


class HlslInterfaceAbiRepairTests(unittest.TestCase):
    def test_recovers_signatures_structured_stride_and_typed_width(self) -> None:
        result = repair_spirv_cross_hlsl_interface(LOWERED, baseline_interface=interface())
        self.assertIn("float4 tint : color;", result)
        self.assertIn("uint prim : primitive_id;", result)
        self.assertLess(result.index("position : sv_position"), result.index("tint : color"))
        self.assertIn("bool __replaylab_unused_frontface_r2 : SV_IsFrontFace;", result)
        self.assertIn("StructuredBuffer<float2> Words", result)
        self.assertIn("Words.Load(index / 2u)[index % 2u]", result)
        self.assertIn("Buffer<float> Scalars", result)
        self.assertIn("__replaylab_load_s0_t0(input.prim * 2u + 1u)", result)

    def test_accepts_bool_frontface_half_and_precise_declarations(self) -> None:
        # SPIRV-Cross declares the captured SV_IsFrontFace row as ``bool``; a
        # hand-annotated struct may carry comments and 16-bit spellings.
        declared = LOWERED.replace(
            "float4 position : SV_Position;",
            "float4 position : SV_Position; // clip position\n"
            "/* front facing */ bool front : SV_IsFrontFace;\n"
            "precise min16float2 packed : TEXCOORD5;",
        )
        rows = interface()
        rows["inputSignature"][-1]["channelUsedMask"] = 2
        rows["inputSignature"].append({
            "semanticName": "uv", "semanticIdxName": "uv", "semanticIndex": 0,
            "regIndex": 5, "regChannelMask": 3, "channelUsedMask": 3,
            "compCount": 2, "compType": "float", "systemValue": "undefined"})
        result = repair_spirv_cross_hlsl_interface(declared, baseline_interface=rows)
        self.assertIn("bool front : sv_isfrontface;", result)
        self.assertIn("precise min16float2 packed : uv;", result)
        self.assertNotIn("__replaylab_unused_frontface", result)
        self.assertNotIn("clip position", result)

    def test_reports_the_unsupported_declaration_text(self) -> None:
        broken = LOWERED.replace("float4 position : SV_Position;", "float4x4 m : TEXCOORD7;")
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "tail: 'float4x4 m : TEXCOORD7;'"):
            repair_spirv_cross_hlsl_interface(broken, baseline_interface=interface())

    def test_refuses_missing_used_inputs_and_ambiguous_structures(self) -> None:
        changed = interface()
        changed["inputSignature"][-1]["channelUsedMask"] = 2
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "omitted a captured input"):
            repair_spirv_cross_hlsl_interface(LOWERED, baseline_interface=changed)
        changed = copy.deepcopy(interface())
        changed["readOnlyResources"][0]["variableType"]["byteSize"] = 12
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "narrow recovery"):
            repair_spirv_cross_hlsl_interface(LOWERED, baseline_interface=changed)

    def test_refuses_unsupported_lowering_and_unbound_inputs(self) -> None:
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "one uint word"):
            repair_spirv_cross_hlsl_interface(LOWERED.replace("2u + 1u).x", "2u + 1u).y"), baseline_interface=interface())
        with self.assertRaisesRegex(HlslCBufferAbiRepairError, "one captured physical binding"):
            repair_spirv_cross_hlsl_interface(LOWERED.replace("TEXCOORD1", "TEXCOORD3"), baseline_interface=interface())
