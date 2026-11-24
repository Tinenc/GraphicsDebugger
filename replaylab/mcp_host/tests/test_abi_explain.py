from __future__ import annotations

import unittest

from src.shader_workspace.abi_explain import explain_abi_differences


def _row(name, reg, width=4, base="float", mask=15, system="undefined"):
    return {"semanticName": name, "semanticIdxName": name, "semanticIndex": 0,
            "regIndex": reg, "regChannelMask": mask, "compCount": width,
            "compType": base, "systemValue": system, "stream": 0}


class AbiExplainTests(unittest.TestCase):
    def test_findings_are_keyed_by_identity_not_position(self) -> None:
        baseline = {
            "inputSignature": [_row("SV_Position", 0, system="position"), _row("TEXCOORD0", 1),
                               _row("TEXCOORD1", 2), _row("SV_IsFrontFace", 3, 1, "uint", 1, "isfrontface")],
            "outputSignature": [_row("SV_Target0", 0)],
            "constantBlocks": [{"space": 0, "bind": 0, "byteSize": 44, "layoutCompleteness": "complete",
                                "fields": [{"byteOffset": 0, "byteSize": 16, "baseType": "float", "columns": 4},
                                           {"byteOffset": 16, "byteSize": 16, "baseType": "float", "columns": 4},
                                           {"byteOffset": 32, "byteSize": 12, "baseType": "float", "columns": 3}]}],
            "readOnlyResources": [], "readWriteResources": [], "samplers": [],
        }
        candidate = {
            "inputSignature": [_row("SV_Position", 0, system="position"), _row("TEXCOORD1", 1), _row("TEXCOORD0", 2)],
            "outputSignature": [_row("SV_Target0", 0)],
            "constantBlocks": [{"space": 0, "bind": 0, "byteSize": 48, "layoutCompleteness": "complete",
                                "fields": [{"byteOffset": 0, "byteSize": 48, "baseType": "float", "columns": 4, "elements": 3}]}],
            "readOnlyResources": [], "readWriteResources": [], "samplers": [],
        }
        result = explain_abi_differences(baseline, candidate)
        kinds = {(item["area"], item["kind"], item["identity"]) for item in result["findings"]}
        self.assertIn(("inputSignature", "missing-row", "SV_IsFrontFace"), kinds)
        self.assertIn(("inputSignature", "moved-row", "TEXCOORD0"), kinds)
        self.assertIn(("constantBlocks", "size-mismatch", "cbuffer register(b0, space0)"), kinds)
        self.assertIn(("constantBlocks", "member-missing", "cbuffer register(b0, space0) @32"), kinds)
        missing = next(i for i in result["findings"] if i["kind"] == "missing-row")
        self.assertIn("SV_IsFrontFace", missing["hint"])
        self.assertIn("uint", missing["hint"])
        size = next(i for i in result["findings"] if i["kind"] == "size-mismatch")
        self.assertIn("44", size["hint"])
        self.assertTrue(result["available"])
        self.assertIn("inputSignature", result["summary"])

    def test_identical_interfaces_and_missing_input_are_explicit(self) -> None:
        interface = {"inputSignature": [_row("TEXCOORD0", 0)], "outputSignature": [],
                     "constantBlocks": [], "readOnlyResources": [], "readWriteResources": [], "samplers": []}
        self.assertEqual(explain_abi_differences(interface, interface)["findingCount"], 0)
        self.assertFalse(explain_abi_differences(None, interface)["available"])

    def test_opaque_blocks_only_report_size(self) -> None:
        baseline = {"constantBlocks": [{"space": 0, "bind": 1, "byteSize": 64, "layoutCompleteness": "opaque", "fields": []}]}
        candidate = {"constantBlocks": [{"space": 0, "bind": 1, "byteSize": 80, "layoutCompleteness": "complete",
                                         "fields": [{"byteOffset": 0, "byteSize": 80, "baseType": "float", "columns": 4}]}]}
        findings = explain_abi_differences(baseline, candidate)["findings"]
        self.assertEqual([item["kind"] for item in findings], ["size-mismatch"])


if __name__ == "__main__":
    unittest.main()
