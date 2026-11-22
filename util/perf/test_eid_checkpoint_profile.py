"""CPU-only parameter and verdict tests; no RenderDoc module or GPU is loaded."""
import json
import copy
from types import SimpleNamespace
import unittest

from eid_checkpoint_profile import (compare_event, force_bypass_issues, glsl_fragment_fixture, output_subresources,
                                    parse_events, parse_status, selected_event_evidence, shader_encoding_names,
                                    status_issues, resolve_extra_textures, resolve_extra_buffers, full_buffer_witness)


def status(**changes):
    value = dict(schema="renderdoc-replay-checkpoints/v1", byteBudget=100, maxCheckpoints=2,
                 checkpointCount=1, allocatedBytes=80, sharedBytes=20, builds=1, hits=1,
                 misses=1, invalidations=0, rejected=0, enabled=True, events=[10])
    value.update(changes)
    return value


class CheckpointProfileTests(unittest.TestCase):
    def test_full_buffer_witness_checks_all_bytes_and_rejects_short_reads_or_missing_resources(self):
        resource = SimpleNamespace(name='parameter storage', resourceId=12)
        buffer = SimpleNamespace(resourceId=12, length=64)
        selected = resolve_extra_buffers([resource.name], [resource], [buffer])
        calls = []
        contents = bytearray(64)
        def read(resource_id, offset, count):
            calls.append((resource_id, offset, count))
            return contents
        controller = SimpleNamespace(GetBufferData=read)
        original = full_buffer_witness(controller, selected, 64)
        contents[63] = 1
        self.assertNotEqual(original, full_buffer_witness(controller, selected, 64))
        self.assertEqual(calls, [(12, 0, 64)] * 2)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            full_buffer_witness(controller, selected, 63)
        self.assertEqual(len(calls), 2)
        contents.pop()
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            full_buffer_witness(controller, selected, 64)
        for names, resources, buffers in ((['missing'], [resource], [buffer]),
                ([resource.name], [resource, resource], [buffer]),
                ([resource.name], [resource], []), ([resource.name] * 2, [resource], [buffer])):
            with self.assertRaises(ValueError):
                resolve_extra_buffers(names, resources, buffers)
        buffer.length = 0
        with self.assertRaises(ValueError):
            resolve_extra_buffers([resource.name], [resource], [buffer])

    def test_explicit_extra_textures_cover_whole_resource_and_reject_ambiguity(self):
        resource = SimpleNamespace(name="unbound witness", resourceId=71)
        texture = SimpleNamespace(resourceId=71, width=8, height=4, depth=1,
                                  mips=2, arraysize=3, msSamp=2)
        selected = resolve_extra_textures(["unbound witness"], [resource], [texture])
        self.assertEqual(selected, [("unbound witness", texture)])
        full = SimpleNamespace(firstMip=0, numMips=texture.mips, firstSlice=0,
                               numSlices=texture.arraysize)
        self.assertEqual(len(output_subresources(selected[0][1], full)), 12)
        for names, resources, textures in ((["missing"], [resource], [texture]),
                                           (["unbound witness"], [resource, resource], [texture]),
                                           (["unbound witness"], [resource], []),
                                           (["unbound witness"] * 2, [resource], [texture]),
                                           ([""], [resource], [texture])):
            with self.assertRaises(ValueError):
                resolve_extra_textures(names, resources, textures)
        texture.mips = 0
        with self.assertRaises(ValueError):
            resolve_extra_textures(["unbound witness"], [resource], [texture])

    def test_native_integer_encoding_list_uses_loaded_api_values(self):
        # Simulate the native list<int> result, with enum values deliberately not assumed in code.
        api = SimpleNamespace(GLSL=12, OpenGLSPIRV=27, HLSL=31, SPIRV=40)
        self.assertEqual(shader_encoding_names([12, 27], api), ["GLSL", "OpenGLSPIRV"])
        self.assertEqual(shader_encoding_names([40, 12], api), ["SPIRV", "GLSL"])
        self.assertEqual(shader_encoding_names([999], api), ["Unknown(999)"])

    def test_glsl_mutation_requires_reflected_single_float_rt0(self):
        evidence = dict(api="OpenGL", encodings=["GLSL"], stage="Pixel", entryPoint="main",
                        outputSignature=[dict(systemValue="ColorOutput", regIndex=0, varType="Float",
                                              compCount=4, regChannelMask=15, stream=0, perPrimitiveRate=False)],
                        boundColorTargets=[dict(slot=0, componentType="UNormSRGB")])
        self.assertTrue(glsl_fragment_fixture(evidence).startswith("#version 330 core\n"))
        vulkan = copy.deepcopy(evidence)
        vulkan.update(api="Vulkan", encodings=["SPIRV", "GLSL"])
        self.assertTrue(glsl_fragment_fixture(vulkan).startswith("#version 450\n"))
        invalid = []
        for field, value in (("encodings", ["SPIRV"]), ("entryPoint", "unknown"),
                             ("outputSignature", []), ("stage", "Vertex")):
            row = copy.deepcopy(evidence)
            row[field] = value
            invalid.append(row)
        for field, value in (("systemValue", "DepthOutput"), ("regIndex", 1), ("varType", "UInt"),
                             ("compCount", 3), ("regChannelMask", 7), ("stream", 1)):
            row = copy.deepcopy(evidence)
            row["outputSignature"][0][field] = value
            invalid.append(row)
        for targets in ([dict(slot=1, componentType="UNorm")],
                        [dict(slot=0, componentType="UInt")],
                        [dict(slot=0, componentType="UNorm"), dict(slot=1, componentType="UNorm")]):
            row = copy.deepcopy(evidence)
            row["boundColorTargets"] = targets
            invalid.append(row)
        for row in invalid:
            with self.assertRaises(ValueError):
                glsl_fragment_fixture(row)

    def test_api_event_evidence_does_not_substitute_nearest_draw(self):
        chunks = [SimpleNamespace(name="vkCmdBeginRenderPass", metadata=SimpleNamespace(chunkID=1001)),
                  SimpleNamespace(name="vkCmdBindPipeline", metadata=SimpleNamespace(chunkID=1002)),
                  SimpleNamespace(name="vkCmdDraw", metadata=SimpleNamespace(chunkID=1003))]
        api_events = [SimpleNamespace(eventId=eid, chunkIndex=index, fileOffset=100 + index * 16)
                      for index, eid in enumerate((12, 16, 17))]
        action = SimpleNamespace(eventId=17, flags="Drawcall", events=api_events, children=[])
        controller = SimpleNamespace(GetStructuredFile=lambda: SimpleNamespace(chunks=chunks),
                                     GetRootActions=lambda: [action])
        rows = selected_event_evidence(controller, [11, 16, 17])
        self.assertEqual(rows[0]["status"], "noExactAPIEvent")
        self.assertEqual(rows[1]["chunkName"], "vkCmdBindPipeline")
        self.assertEqual(rows[1]["owningActionEventId"], 17)
        self.assertEqual(rows[1]["owningActionContext"][0]["chunkName"], "vkCmdBeginRenderPass")

    def test_event_parameters_require_distinct_bounded_sequence(self):
        self.assertEqual(parse_events("20,10"), [10, 20])
        for text in ("10", "10,10", "0,10", "-1,20", ",".join(str(i) for i in range(1, 10))):
            with self.assertRaises(ValueError):
                parse_events(text)

    def test_status_schema_counters_budget_and_disable(self):
        self.assertEqual(parse_status(json.dumps(status()))["allocatedBytes"], 80)
        self.assertTrue(status_issues(status(allocatedBytes=101)))
        self.assertTrue(status_issues(status(), disabled=True))
        self.assertEqual(status_issues(status(enabled=False, checkpointCount=0, allocatedBytes=0,
                                              sharedBytes=0, events=[]), disabled=True), [])
        with self.assertRaises(ValueError):
            parse_status(json.dumps(status(hits=-1)))

    def test_force_must_neither_build_nor_restore_nor_discard(self):
        before = status()
        self.assertEqual(force_bypass_issues(before, status()), [])
        for change in (dict(hits=2), dict(builds=2), dict(events=[]), dict(allocatedBytes=0)):
            self.assertTrue(force_bypass_issues(before, status(**change)))

    def test_nonempty_wrong_pixels_and_unstable_canonical_cannot_pass(self):
        good = {"bindings": [71], "pixels": [{"bytes": 100, "sha256": "a"}]}
        wrong = {"bindings": [71], "pixels": [{"bytes": 100, "sha256": "b"}]}
        wrong_binding = {"bindings": [72], "pixels": good["pixels"]}
        self.assertEqual(compare_event([good], [good, good]), "passed")
        self.assertEqual(compare_event([wrong], [good, good]), "failedStableCanonicalMismatch")
        self.assertEqual(compare_event([wrong_binding], [good, good]), "failedStableCanonicalMismatch")
        self.assertEqual(compare_event([good], [good, wrong]), "inconclusiveCanonicalUnstable")

    def test_all_view_slices_samples_and_3d_volume_are_covered(self):
        texture = SimpleNamespace(mips=2, arraysize=3, depth=1, msSamp=2)
        binding = SimpleNamespace(firstMip=0, numMips=2, firstSlice=1, numSlices=2)
        self.assertEqual(len(output_subresources(texture, binding)), 8)
        texture.depth = 8
        self.assertEqual(output_subresources(texture, binding), [(0, 0, 0), (0, 0, 1), (1, 0, 0), (1, 0, 1)])

    def test_invalid_output_range_preserves_actual_metadata(self):
        texture = SimpleNamespace(resourceId="ResourceId::71", mips=1, arraysize=1, depth=1, msSamp=1)
        binding = SimpleNamespace(resource="ResourceId::71", firstMip=0, numMips=0, firstSlice=0, numSlices=1)
        with self.assertRaises(ValueError) as failed:
            output_subresources(texture, binding)
        details = json.loads(str(failed.exception).split(" range: ", 1)[1])
        self.assertEqual(details["binding"]["numMips"], "0")
        self.assertEqual(details["texture"]["mips"], "1")
        self.assertEqual(details["binding"]["resource"], "ResourceId::71")
        binding.numMips, binding.firstSlice = 1, 1
        with self.assertRaisesRegex(ValueError, "Invalid bound output slice range"):
            output_subresources(texture, binding)


if __name__ == "__main__":
    unittest.main()
