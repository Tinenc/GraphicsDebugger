"""CPU-only metadata sizing tests; do not import RenderDoc or open captures."""
from types import SimpleNamespace
import unittest

from eid_checkpoint_inventory import (action_metadata, boundary_set, category,
                                      choose_boundaries, marker_category, usage_kind,
                                      usage_summary, usage_window, parse_windows)


def resource(key, size, usage, writable=False):
    return {"resource": key, "name": key, "kind": "buffer", "logicalBytes": size,
            "writeCapableCreationFlag": writable, "usage": usage_summary(usage)}


class CheckpointInventoryTests(unittest.TestCase):
    def test_window_excludes_checkpoint_includes_target_and_clear_is_not_overwrite_proof(self):
        result = usage_window([(10, "PS_Resource"), (15, "Clear"), (15, "PS_Resource"),
                               (20, "PS_Resource"), (21, "CS_RWResource")], 10, 20)
        self.assertEqual(result["usageCount"], 3)
        self.assertEqual(result["firstUseKinds"], ["Clear", "PS_Resource"])
        self.assertTrue(result["startsWithReportedClear"])
        self.assertFalse(result["fullOverwriteProven"])
        self.assertFalse(result["safeToSkipRestore"])
        self.assertIsNone(result["byteRanges"])

    def test_empty_window_and_unknown_usage_never_authorize_skip(self):
        result = usage_window([(40, "PS_Resource")], 10, 20)
        self.assertTrue(result["noReportedUse"])
        self.assertFalse(result["safeToSkipRestore"])
        unknown = usage_window([(15, "FutureUnknownUse")], 10, 20)
        self.assertFalse(unknown["noReportedUse"])
        self.assertEqual(unknown["unknownUsageKinds"], ["FutureUnknownUse"])

    def test_window_arguments_are_bounded_and_valid(self):
        self.assertEqual(parse_windows("0:10,5:20,0:10"), [(0, 10), (5, 20)])
        for value in ("1:1", "-1:20", "1:2:3", "1", "x:2"):
            with self.assertRaises(ValueError):
                parse_windows(value)
        with self.assertRaises(ValueError):
            parse_windows("0:10,1:20", maximum=1)

    def test_usage_classifies_cpu_write_and_read_write_as_possible_write(self):
        for name in ("CPUWrite", "CS_RWResource", "Copy", "ResolveDst", "Discard", "StreamOut"):
            self.assertEqual(usage_kind(name), "potentialWrite")
        for name in ("ResourceUsage.PS_Constants", "VS_Resource", "CopySrc", "InputTarget"):
            self.assertEqual(usage_kind(name), "read")
        self.assertEqual(usage_kind("Barrier"), "neutral")
        self.assertEqual(usage_kind("FutureUnknownWrite"), "unknown")

    def test_boundary_is_after_event_and_retains_later_overwrites(self):
        entries = [resource("readOnly", 10, [(1, "PS_Resource"), (30, "PS_Resource")]),
                   resource("live", 20, [(10, "ColorTarget"), (20, "PS_Resource")]),
                   resource("lastUseAtBoundary", 40, [(1, "CPUWrite"), (10, "VS_Constants")]),
                   resource("futureOnly", 80, [(11, "CopyDst"), (30, "PS_Resource")]),
                   resource("laterOverwrite", 160, [(5, "CopyDst"), (30, "CopyDst")])]
        result = boundary_set(entries, 10)
        self.assertEqual(result["resources"], ["live", "laterOverwrite"])
        self.assertEqual(result["logicalBytes"], 180)
        self.assertIsNone(result["completeCheckpointBytes"])
        self.assertFalse(result["safetyValidated"])

    def test_unknown_and_unreported_writable_never_become_read_only(self):
        self.assertEqual(category(resource("a", 1, [(2, "PS_Resource")])), "observedReadOnlyCandidate")
        self.assertEqual(category(resource("b", 1, [(2, "PS_Resource")], True)), "writeCapableWithoutReportedWrite")
        self.assertEqual(category(resource("c", 1, [(2, "NewUnknownUsage")])), "unknownUsage")
        missing = resource("d", 1, [])
        del missing["usage"]
        self.assertEqual(category(missing), "usageNotAvailable")

    def test_constants_are_resource_wide_and_earliest_consumption_preserved(self):
        result = usage_summary([(20, "PS_Constants"), (3, "VS_Constants"),
                                (8, "CPUWrite"), (20, "PS_Constants")])
        self.assertEqual(result["firstConstantConsumption"], 3)
        self.assertEqual(result["constantConsumptionCount"], 2)
        self.assertEqual(result["constantConsumptionSample"], [3, 20])

    def test_markers_and_actual_output_groups_remain_candidates(self):
        flag_names = ("Drawcall", "MeshDispatch", "PushMarker", "SetMarker", "Dispatch", "DispatchRay",
                      "Clear", "Copy", "Resolve", "GenMips", "Present", "BuildAccStruct")
        flags = SimpleNamespace(**{name: 1 << i for i, name in enumerate(flag_names)})
        rd = SimpleNamespace(ActionFlags=flags)
        def action(eid, flag, children=(), name="", outputs=(), depth="ResourceId::0"):
            return SimpleNamespace(eventId=eid, flags=flag, customName=name, children=children,
                                   outputs=outputs, depthOut=depth)
        roots = [action(1, flags.PushMarker, name="DepthPrepass", children=[
            action(2, flags.Drawcall, depth="ResourceId::5"),
            action(3, flags.Copy), action(4, flags.Drawcall, depth="ResourceId::5")]),
            action(8, flags.Drawcall, outputs=("ResourceId::6", "ResourceId::7"))]
        result = action_metadata(roots, rd)
        self.assertEqual(result["markers"][0]["lastActionEvent"], 4)
        self.assertEqual(result["markers"][0]["inferredCategory"], "depth/prepass")
        self.assertEqual(len(result["outputGroups"]), 3)  # copy interrupts two identical bindings
        self.assertEqual(result["outputGroups"][-1]["outputClassification"], "multiple-color-output candidate")
        chosen = choose_boundaries(result, [2], 3)
        self.assertLessEqual(len(chosen), 3)
        self.assertEqual(next(b for b in chosen if b["eventId"] == 2)["source"], "explicit requested event")
        self.assertIsNone(marker_category("Some opaque engine marker"))


if __name__ == "__main__":
    unittest.main()
