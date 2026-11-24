"""Channel meaning comes from the vertex shader; bad specs fail once, not nine times."""

from __future__ import annotations

import unittest

from src.resource_export import ResourceExportManager, ResourceExportError


class PassthroughContractTests(unittest.TestCase):
    """The Host carries unknown channels through; it does not guess what they mean."""

    def test_no_value_based_inference_exists_any_more(self) -> None:
        # Channel meaning comes from the vertex shader, read by the Agent. A statistical
        # guess in the Host is exactly the failure mode this export had.
        self.assertFalse(hasattr(ResourceExportManager, "_infer_attribute_targets"))
        self.assertFalse(hasattr(ResourceExportManager, "_channel_statistics"))

    def test_declared_semantics_are_honoured_and_anonymous_slots_are_not_renamed(self) -> None:
        self.assertEqual(ResourceExportManager._automatic_target({"semantic": "POSITION"}), "POSITION")
        self.assertEqual(ResourceExportManager._automatic_target({"semantic": "TEXCOORD1"}), "TEXCOORD_1")
        # An anonymous stream produces a custom target, which the writer turns into
        # _ATTR<slot>; nothing claims it is a position or a normal.
        anonymous = ResourceExportManager._automatic_target({"name": "ATTRIBUTE0", "location": 0})
        self.assertTrue(anonymous.startswith("_"))
        self.assertNotIn("POSITION", anonymous)

    def test_an_explicit_mapping_always_wins(self) -> None:
        mappings = [{"source": {"name": "ATTRIBUTE0"}, "target": "POSITION"}]
        self.assertEqual(
            ResourceExportManager._mapping_for({"name": "ATTRIBUTE0"}, mappings), "POSITION")
        self.assertIsNone(ResourceExportManager._mapping_for({"name": "ATTRIBUTE7"}, mappings))

    def test_attribute_index_selects_only_that_layout_entry(self) -> None:
        mappings = [{"source": {"attributeIndex": 3}, "target": "NORMAL"}]
        self.assertIsNone(ResourceExportManager._mapping_for({"index": 0}, mappings))
        self.assertEqual(ResourceExportManager._mapping_for({"index": 3}, mappings), "NORMAL")
        normalized = ResourceExportManager.from_environment({})._normalize_transfer_spec({
            "targets": ["gltf"], "attributeMappings": mappings})
        self.assertEqual(normalized["attributeMappings"][0]["source"], {"index": 3})

    def test_conflicting_index_alias_is_rejected(self) -> None:
        with self.assertRaises(ResourceExportError):
            ResourceExportManager.from_environment({})._normalize_transfer_spec({
                "attributeMappings": [{"source": {"attributeIndex": 0, "index": 1}, "target": "POSITION"}]})


class TransferSpecProblemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ResourceExportManager.from_environment({})

    def test_every_problem_is_reported_together(self) -> None:
        problems = self.manager._transfer_spec_problems({
            "targets": ["gltf", "obj", "usd"],
            "attributeMappings": [
                {"target": "POSITION"},
                {"source": {"name": "a"}},
                {"source": {"name": "b"}, "target": "WORLDPOS"},
                {"source": {"name": "c"}, "target": "POSITION"},
                {"source": {"name": "d"}, "target": "position"},
            ],
            "textureMappings": [{"role": "baseColor"}],
        })
        joined = " | ".join(problems)
        self.assertIn("obj", joined)
        self.assertIn("usd", joined)
        self.assertIn("attributeMappings[0] needs a source", joined)
        self.assertIn("attributeMappings[1] needs a target", joined)
        self.assertIn("WORLDPOS", joined)
        self.assertIn("repeats the target POSITION", joined)
        self.assertIn("sourceResourceId", joined)

    def test_a_valid_specification_reports_nothing(self) -> None:
        self.assertEqual(self.manager._transfer_spec_problems({
            "targets": ["gltf", "fbx"],
            "attributeMappings": [{"source": {"name": "POSITION0"}, "target": "POSITION"}],
            "textureMappings": [{"sourceResourceId": 333, "role": "baseColor"}],
        }), [])

    def test_obj_is_refused_and_fbx_is_a_normal_target(self) -> None:
        problems = self.manager._transfer_spec_problems({"targets": ["obj"]})
        self.assertTrue(any("skinning" in item for item in problems))
        self.assertEqual(self.manager._transfer_spec_problems({"targets": ["gltf", "fbx"]}), [])


if __name__ == "__main__":
    unittest.main()
