"""Extract local pipes stay unique under later rename ledger pressure."""

from __future__ import annotations

import unittest

from src.shader_workspace.equivalence import apply_renames, validate_renames
from src.shader_workspace.restructure import extract_function


class ExtractLocalUniquenessTests(unittest.TestCase):
    def test_extract_renames_helper_locals_away_from_decompiled_ids(self) -> None:
        source = "\n".join([
            "float4 MainPS(float4 input : TEXCOORD0) : SV_Target",
            "{",
            "    float _12 = input.x;",
            "    float _13 = input.y;",
            "    return float4(_12, _13, 0.0f, 1.0f);",
            "}",
            "",
        ])
        result = extract_function(source, 3, 4, "SplitChannels")
        text = result["source"]
        self.assertIn("out float replaylab_out__12", text)
        self.assertIn("float replaylab_local__12", text)
        self.assertIn("replaylab_out__12 = replaylab_local__12", text)
        self.assertNotIn("out float _12", text)
        # Caller keeps the decompiled id once; helper no longer declares it.
        self.assertIn("float _12;", text)
        self.assertEqual(text.count("float _12"), 1)

    def test_rename_out_param_back_to_decompiled_id_is_auto_disambiguated(self) -> None:
        # Legacy conflict shape: helper still has local `_12` and out-param
        # `replaylab_out__12`. Renaming the out-param onto `_12` must not be
        # accepted just because leaf `_12` appears elsewhere in the ledger.
        source = "\n".join([
            "void SplitChannels(float4 _11, out float replaylab_out__12)",
            "{",
            "    float _12 = _11.x;",
            "    replaylab_out__12 = _12;",
            "}",
            "float4 MainPS(float4 input : TEXCOORD0) : SV_Target",
            "{",
            "    float _12;",
            "    SplitChannels(input, _12);",
            "    return _12.xxxx;",
            "}",
            "",
        ])
        clean = validate_renames(
            source,
            {
                "MainPS::_12": "channelX",
                "SplitChannels::replaylab_out__12": "_12",
            },
            known_old_leaves=["_12"],
        )
        self.assertEqual(clean["MainPS::_12"], "channelX")
        self.assertNotEqual(clean["SplitChannels::replaylab_out__12"], "_12")
        proposal = apply_renames(source, clean)
        out_name = clean["SplitChannels::replaylab_out__12"]
        again = validate_renames(
            proposal,
            {"SplitChannels::%s" % out_name: "outChannelX"},
        )
        self.assertEqual(again["SplitChannels::%s" % out_name], "outChannelX")


if __name__ == "__main__":
    unittest.main()
