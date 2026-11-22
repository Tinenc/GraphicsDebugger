"""CPU-only evidence tests. No RenderDoc import, capture, GPU, or demo execution."""
import copy
import unittest

from discover_d3d12_checkpoint_fixture import binding_issues, select_fixture_events


def action(event, kind, name=""):
    return {"eventId": event, "name": name, "commandBoundary": kind in ("begin", "end"),
            "beginPass": kind == "begin", "endPass": kind == "end", "draw": kind == "draw",
            "clear": kind == "clear", "marker": kind == "marker",
            "exactAPI": {"chunkName": "ID3D12GraphicsCommandList::" +
                         ("DrawInstanced" if kind == "draw" else "SetMarker" if kind == "marker"
                          else "ClearRenderTargetView")}}


def group(start):
    return [action(start, "begin", "=> ExecuteCommandLists(1)[0]: Reset(ResourceId::100)"),
            action(start + 2, "clear"), action(start + 13, "draw"),
            action(start + 15, "end", "=> ExecuteCommandLists(1)[0]: Close(ResourceId::100)")]


def alias_actions(mode):
    rows = []
    for region in range(4):
        start = 100 * region + 1
        rows.append(action(start, "begin", "=> ExecuteCommandLists(1)[0]: Reset(ResourceId::100)"))
        if region == 0:
            rows.append(action(start + 1, "marker", "Checkpoint alias fixture " + mode))
        if region >= 2:
            rows.append(action(start + 2, "marker", "Checkpoint alias barrier region " + str(region)))
            if mode == "read":
                rows.extend((action(start + 4, "marker", "Checkpoint alias read-before-clear region " + str(region)),
                             action(start + 5, "draw")))
            rows.extend((action(start + 7, "marker", "Checkpoint alias full-clear region " + str(region)),
                         action(start + 8, "clear")))
        rows.extend((action(start + 10, "marker", "Checkpoint region " + str(region)),
                     action(start + 11, "draw"),
                     action(start + 15, "end", "=> ExecuteCommandLists(1)[0]: Close(ResourceId::100)")))
    return rows


class FixtureDiscoveryTests(unittest.TestCase):
    def test_explicit_alias_modes_select_only_four_main_draws(self):
        for mode in ("clear", "read", "split"):
            result = select_fixture_events(alias_actions(mode), checkpoint_alias_fixture=mode)
            self.assertEqual(result["selectedEvents"], [12, 112, 212, 312])
            self.assertEqual(result["aliasFixtureEvidence"]["readDraws"], [206, 306] if mode == "read" else [])
            self.assertTrue(result["readyForCheckpointMutation"])
        with self.assertRaisesRegex(ValueError, "one bounded visible draw"):
            select_fixture_events(alias_actions("read"))

    def test_alias_marker_evidence_mode_and_read_order_are_required(self):
        for mode in ("clear", "read", "split"):
            with self.assertRaisesRegex(ValueError, "markers"):
                select_fixture_events(alias_actions(mode), checkpoint_alias_fixture="read" if mode != "read" else "clear")
        rows = alias_actions("read")
        rows = [row for row in rows if row["name"] != "Checkpoint alias read-before-clear region 2"]
        with self.assertRaises(ValueError):
            select_fixture_events(rows, checkpoint_alias_fixture="read")
        rows = alias_actions("read")
        next(row for row in rows if row["name"] == "Checkpoint region 2")["exactAPI"] = None
        with self.assertRaisesRegex(ValueError, "SetMarker"):
            select_fixture_events(rows, checkpoint_alias_fixture="read")
        rows = alias_actions("read")
        next(row for row in rows if row["eventId"] == 206)["eventId"] = 210
        with self.assertRaises(ValueError):
            select_fixture_events(rows, checkpoint_alias_fixture="read")

    def test_alias_never_accepts_missing_submissions_or_unknown_extra_draws(self):
        with self.assertRaisesRegex(ValueError, "exactly four"):
            select_fixture_events([row for row in alias_actions("clear") if row["eventId"] < 301],
                                  checkpoint_alias_fixture="clear")
        rows = alias_actions("read") + [action(109, "draw")]
        with self.assertRaisesRegex(ValueError, "draw count"):
            select_fixture_events(rows, checkpoint_alias_fixture="read")
        with self.assertRaisesRegex(ValueError, "single-submit"):
            select_fixture_events(alias_actions("read"), True, "read")

    def test_selects_actual_draw_eids_across_completed_submissions(self):
        report = select_fixture_events(group(18) + group(57) + group(91))
        self.assertEqual(report["selectedEvents"], [31, 70, 104])
        self.assertTrue(report["readyForCheckpointMutation"])

    def test_single_submit_is_explicitly_not_mutation_reuse_evidence(self):
        with self.assertRaises(ValueError):
            select_fixture_events(group(100))
        report = select_fixture_events(group(100), True)
        self.assertEqual(report["selectedEvents"], [102, 113])
        self.assertFalse(report["readyForCheckpointMutation"])

    def test_unknown_api_or_incomplete_group_never_guesses_eid(self):
        rows = group(1) + group(50)
        rows[2]["exactAPI"] = None
        with self.assertRaises(ValueError):
            select_fixture_events(rows)
        with self.assertRaises(ValueError):
            select_fixture_events((group(1) + group(50))[:-1])
        rows = group(1) + group(50)
        rows[0]["name"] = "=> ExecuteCommandLists(2)[0]: Reset(ResourceId::100)"
        with self.assertRaises(ValueError):
            select_fixture_events(rows)

    def test_requires_shared_real_shader_rt_and_float_output(self):
        first = {"eventId": 3, "pixelShader": "ResourceId::37", "colorTargets": [{"slot": 0, "resource": "ResourceId::11"}],
                 "shaderOutputSignature": [{"systemValue": "ColorOutput", "regIndex": 0, "compCount": 4, "varType": "Float"}]}
        second = copy.deepcopy(first)
        second["eventId"] = 77
        self.assertEqual(binding_issues([first, second], {3, 77}), [])
        second["pixelShader"] = "ResourceId::38"
        self.assertIn("selected submissions must share the actual captured pixel shader", binding_issues([first, second], {3, 77}))
        second = copy.deepcopy(first)
        second["eventId"] = 77
        second["shaderOutputSignature"][0]["varType"] = "UInt"
        self.assertTrue(binding_issues([first, second], {3, 77}))


if __name__ == "__main__":
    unittest.main()
