from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from tests.test_bridge_dxil_access_gate import ResourceId, load_bridge, reflection
from tests.test_dxil_access_signature import REAL_SHAPE_DXIL


class ShaderRestoreReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / "same.rdc"
        self.capture.write_bytes(b"immutable captured frame")
        self.candidate = self.root / "candidate.dxil"
        self.candidate.write_bytes(b"compiled candidate")
        self.original = b"captured original DXIL"
        self.catalog = {
            "projection": {"captureId": "c" * 64, "workspaceId": "b" * 32},
            "scope": {
                "identityEvidence": {"selection": "fixture-original"},
                "stableIdentityEvidence": {"selection": "fixture-stable"},
                "stableCaptureId": "e" * 64,
            },
        }

    def bridge(self, resource_id=77, session="7" * 32):
        module = load_bridge()
        original = self.original
        capture_path = str(self.capture)

        class Controller:
            def __init__(self):
                self.api = "GraphicsAPI.D3D12"
                self.original = original
                self.calls = []
                self.replacements = {}
                self.targets = {}

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType=self.api)

            def SetFrameEvent(self, event, force):
                self.calls.append(("event", event))

            def GetPipelineState(self):
                return types.SimpleNamespace(
                    GetShader=lambda stage: ResourceId(resource_id),
                    GetShaderEntryPoint=lambda stage: "main",
                    GetGraphicsPipelineObject=lambda: ResourceId(5),
                )

            def GetShaderEntryPoints(self, shader):
                self.calls.append(("entries", int(shader)))
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                value = int(shader)
                self.calls.append(("reflect", value))
                value = self.replacements.get(value, value)
                blob = self.original if value == resource_id else self.targets.get(value)
                return reflection(blob) if blob is not None else None

            def GetTargetShaderEncodings(self):
                return ["DXIL"]

            def BuildTargetShader(self, entry, encoding, data, flags, stage):
                self.calls.append(("build", bytes(data)))
                target = 100 + len(self.targets)
                self.targets[target] = bytes(data)
                return ResourceId(target), ""

            def ReplaceTargetShaderChecked(self, original_id, target):
                self.calls.append(("replace", int(original_id), int(target)))
                self.replacements[int(original_id)] = int(target)
                return ""

            def RemoveTargetShaderReplacementChecked(self, original_id):
                self.replacements.pop(int(original_id), None)
                return ""

            def FreeTargetResource(self, target):
                self.calls.append(("free", int(target)))

        class Context:
            def __init__(self):
                self.replacements = {}

            def IsCaptureLoaded(self):
                return True

            def CurEvent(self):
                return 321

            def GetCaptureFilename(self):
                return capture_path

            def GetResources(self):
                return [types.SimpleNamespace(resourceId=ResourceId(resource_id))]

            def IsResourceReplaced(self, original_id):
                return int(original_id) in self.replacements

            def GetResourceReplacement(self, original_id):
                return self.replacements.get(int(original_id), ResourceId())

            def RegisterReplacement(self, original_id, target):
                self.replacements[int(original_id)] = target

            def UnregisterReplacement(self, original_id):
                self.replacements.pop(int(original_id), None)

        controller = Controller()
        module._ctx = Context()
        module._capture_session_id = session
        module._replay = lambda callback: callback(controller)
        module._shader_restore_receipt_store = module._shader_restore_protocol.ReceiptStore(str(self.root / "keys"))
        return module, controller

    def params(self):
        return {
            "candidate_path": str(self.candidate),
            "expected_sha256": hashlib.sha256(self.candidate.read_bytes()).hexdigest(),
            "expected_capture_id": "c" * 64,
            "event_id": 321,
            "stage": "fragment",
            "entry_point": "main",
            "encoding": "dxil",
            "expected_original_resource_id": 77,
            "expected_original_sha256": hashlib.sha256(self.original).hexdigest(),
            "workspace_id": "b" * 32,
            "revision": 1,
            "abi_request_id": "a" * 64,
            "abi_report_sha256": "d" * 64,
            "expected_source_sha256": "f" * 64,
            "workspace_catalog_binding": copy.deepcopy(self.catalog),
        }

    def test_reviewed_mapping_schedule_retains_prior_proof_without_accepting_other_changes(self):
        module, unused = self.bridge()
        current = module._shader_restore_physical_validation_binding
        self.assertIsNotNone(current)
        previous = copy.deepcopy(current)
        row = next(row for row in previous['algorithms'] if row['role'] == 'bridge-physical-shader-validation')
        row['codeSha256'] = 'f67a38aa325d67137a43386b4eeb0e23b5734939b121d5fe2149ec653ecc2ecd'
        protocol = module._shader_restore_protocol
        self.assertTrue(protocol.compatible_validation_binding(previous, current))
        self.assertIn(protocol.physical_validation_binding_sha256(previous),
                      protocol.compatible_validation_policies(current))
        changed = copy.deepcopy(current)
        changed['algorithms'][1]['codeSha256'] = 'd' * 64
        self.assertFalse(protocol.compatible_validation_binding(previous, changed))
        changed = copy.deepcopy(current)
        changed['nativeBinaries'].append({'name': 'different.dll', 'sha256': 'e' * 64})
        self.assertFalse(protocol.compatible_validation_binding(previous, changed))

    def captured_dxil_evidence(self, module):
        blocks = [{"space": 0, "bind": block.fixedBindNumber, "byteSize": block.byteSize}
                  for block in reflection(self.original).constantBlocks]
        return module._dxil_evidence_from_snapshot(REAL_SHAPE_DXIL, blocks)

    def fully_applied_receipt(self):
        module, controller = self.bridge()
        module._known_catalog_capture_ids = {"c" * 64: os.path.normcase(str(self.capture))}
        exact, stable = module._shader_restore_catalog_pairs(self.params())
        module._known_catalog_identity_pairs.add(exact)
        module._known_catalog_stable_pairs.add(stable)
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", return_value=self.captured_dxil_evidence(module)) as disassemble, \
                mock.patch.object(module, "_dxil_ambiguous_interface_compatible", return_value=True) as compare:
            result = module._apply_shader_candidate_shared_resource(self.params())
        self.assertTrue(result["configured"])
        self.assertEqual(disassemble.call_count, 1)
        self.assertEqual(compare.call_count, 1)
        self.assertIn(("reflect", 100), controller.calls)
        self.assertNotIn("restoreValidationReceiptError", result)
        return result["restoreValidationReceipt"]

    def warm_params(self, receipt):
        return dict(self.params(), abi_validation_mode="verified-rdc-restore",
                    restore_validation_receipt=receipt, expected_capture_session_id="8" * 32,
                    expected_capture_path=str(self.capture),
                    expected_capture_sha256=hashlib.sha256(self.capture.read_bytes()).hexdigest())

    def test_new_bridge_restores_exact_native_bytes_without_dxil_or_target_reflection(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        params = self.warm_params(receipt)
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", side_effect=AssertionError("no DXIL printer")), \
                mock.patch.object(module, "_physical_shader_interface_sha256", side_effect=AssertionError("no repeated ABI")), \
                mock.patch.object(module, "_dxil_ambiguous_interface_compatible", side_effect=AssertionError("no repeated candidate comparison")):
            result = module._apply_shader_candidate_shared_resource(params)
        self.assertTrue(result["configured"])
        self.assertEqual(result["originalResourceId"], 91)
        self.assertEqual(result["restoredOriginalResourceId"], 77)
        self.assertEqual(result["restoreValidationReceipt"], receipt)
        self.assertFalse(result["freshPhysicalAbiValidationPerformed"])
        self.assertIn(("replace", 91, 100), controller.calls)
        self.assertNotIn(("reflect", 100), controller.calls)
        self.assertEqual(int(module._ctx.replacements[91]), 100)
        exact, stable = module._shader_restore_catalog_pairs(params)
        self.assertIn(exact, module._known_catalog_identity_pairs)
        self.assertIn(stable, module._known_catalog_stable_pairs)
        self.assertEqual(module._known_catalog_capture_ids["c" * 64], os.path.normcase(str(self.capture)))

    def test_same_native_resource_restores_without_anchor_replay(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(session="8" * 32)
        result = module._apply_shader_candidate_shared_resource(self.warm_params(receipt))
        self.assertTrue(result["configured"])
        self.assertEqual([call for call in controller.calls if call[0] == "event"], [])
        self.assertIn(("replace", 77, 100), controller.calls)

    def test_resource_only_reflection_unavailable_falls_back_to_anchor(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(session="8" * 32)
        module._get_pipeline_obj = lambda *unused: ResourceId(5)
        read = controller.GetShader
        def without_null_pipeline(pipeline, shader, entry):
            if int(pipeline) == 0:
                raise RuntimeError("resource-only reflection unavailable")
            return read(pipeline, shader, entry)
        controller.GetShader = without_null_pipeline
        result = module._apply_shader_candidate_shared_resource(self.warm_params(receipt))
        self.assertTrue(result["configured"])
        self.assertTrue(any(call[0] == "event" for call in controller.calls))

    def test_verified_idempotent_apply_reports_this_request_without_rewriting_full_owner(self):
        module, controller = self.bridge(session="8" * 32)
        module._known_catalog_capture_ids["c" * 64] = os.path.normcase(str(self.capture))
        exact, stable = module._shader_restore_catalog_pairs(self.params())
        module._known_catalog_identity_pairs.add(exact)
        module._known_catalog_stable_pairs.add(stable)
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly",
                               return_value=self.captured_dxil_evidence(module)), \
             mock.patch.object(module, "_dxil_ambiguous_interface_compatible", return_value=True):
            full = module._apply_shader_candidate_shared_resource(self.params())
        owner = module._shared_shader_replacements[77]
        prior_mode = owner["abiValidationMode"]
        prior_fresh_validation = owner["freshPhysicalAbiValidationPerformed"]
        self.assertTrue(prior_fresh_validation)
        params = dict(self.warm_params(full["restoreValidationReceipt"]), operation_id="4" * 32)
        before_builds = [call for call in controller.calls if call[0] == "build"]
        with mock.patch.object(module, "_physical_shader_interface_sha256",
                               side_effect=AssertionError("must reuse signed ABI")), \
             mock.patch.object(module, "_dxil_evidence_from_live_disassembly",
                               side_effect=AssertionError("must reuse captured DXIL evidence")):
            # The second call exercises the operation-token replay response too.
            for unused in range(2):
                result = module._apply_shader_candidate_shared_resource(params)
                self.assertTrue(result["idempotent"])
                self.assertFalse(result["operationCreatedReplacement"])
                self.assertEqual(result["operationId"], params["operation_id"])
                self.assertEqual(result["abiValidationMode"], "verified-rdc-restore")
                self.assertFalse(result["freshPhysicalAbiValidationPerformed"])
                self.assertEqual(result["restoreValidationReceipt"], params["restore_validation_receipt"])
                self.assertEqual(owner["abiValidationMode"], prior_mode)
                self.assertIs(owner["freshPhysicalAbiValidationPerformed"], prior_fresh_validation)
        self.assertEqual([call for call in controller.calls if call[0] == "build"], before_builds)

    def test_warm_restore_retains_original_evidence_for_catalog_and_next_edit(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        module._active_capture_token = module._current_capture_token()
        controller.GetAPIProperties = lambda: types.SimpleNamespace(
            pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12")
        controller.GetFrameInfo = lambda: types.SimpleNamespace(frameNumber=1)
        controller.GetDisassemblyTargets = lambda *args: ["DXC DXIL"]
        controller.DisassembleShader = lambda *args: REAL_SHAPE_DXIL.decode("utf-8")
        before = module._export_shader_catalog({
            "output_dir": str(self.root / "before"), "event_ids": [321], "stages": ["fragment"]})
        expected_access, expected_layout = self.captured_dxil_evidence(module)
        with mock.patch.object(controller, "DisassembleShader", side_effect=AssertionError("candidate printer must not be called")), \
             mock.patch.object(module, "_dxil_evidence_from_live_disassembly", side_effect=AssertionError("must reuse original evidence")):
            module._apply_shader_candidate_shared_resource(self.warm_params(receipt))
            owner = module._shared_shader_replacements[91]
            self.assertEqual(owner["capturedDxilAccessSignature"].to_dict(), expected_access.to_dict())
            self.assertEqual(owner["capturedDxilLayoutEvidence"], expected_layout)
            after = module._export_shader_catalog({
                "output_dir": str(self.root / "after"), "event_ids": [321], "stages": ["fragment"],
                "expected_stable_capture_id": before["stableCaptureId"],
                "expected_stable_identity_evidence_sha256": before["stableIdentityEvidenceSha256"]})
            before_catalog = json.loads(Path(before["catalogPath"]).read_text(encoding="utf-8"))
            after_catalog = json.loads(Path(after["catalogPath"]).read_text(encoding="utf-8"))
            self.assertEqual(after_catalog["errors"], [])
            self.assertEqual(after_catalog["shaders"], before_catalog["shaders"])
            self.candidate.write_bytes(b"next live edit candidate")
            edited = module._apply_shader_candidate_shared_resource(dict(
                self.params(), revision=2, expected_original_resource_id=91))
        self.assertTrue(edited["configured"])
        self.assertIn("restoreValidationReceipt", edited)
        self.assertEqual(edited["physicalAbiSha256"], receipt["payload"]["physicalAbiSha256"])
        self.assertEqual(controller.replacements[91], 101)
        self.assertEqual(edited["restoreValidationReceipt"]["payload"]["capturedDxilEvidence"],
                         receipt["payload"]["capturedDxilEvidence"])

    def test_dxil_evidence_is_signed_and_required_before_warm_mutation(self):
        receipt = self.fully_applied_receipt()
        for change in ("unsigned-layout", "missing", "wrong-source", "wrong-signature", "excess-accesses"):
            with self.subTest(change=change):
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                changed = copy.deepcopy(receipt)
                evidence = changed["payload"]["capturedDxilEvidence"]
                if change == "missing":
                    del changed["payload"]["capturedDxilEvidence"]
                elif change in ("unsigned-layout", "wrong-source"):
                    evidence["layoutEvidence"][0]["evidence"]["sourceSha256"] = "0" * 64
                elif change == "wrong-signature":
                    evidence["accessSignature"]["blocks"][0]["accesses"][0]["byteOffset"] += 4
                else:
                    accesses = evidence["accessSignature"]["blocks"][0]["accesses"]
                    accesses[:] = [accesses[0]] * (module._shader_restore_protocol.MAX_DXIL_EVIDENCE_ACCESSES + 1)
                if change != "unsigned-layout":
                    changed = module._shader_restore_receipt_store.seal(changed["payload"])
                with self.assertRaises((RuntimeError, ValueError)):
                    module._apply_shader_candidate_shared_resource(self.warm_params(changed))
                self.assertFalse(controller.calls)
                self.assertEqual(controller.replacements, {})

    def test_oversized_future_evidence_does_not_fail_a_successful_full_apply(self):
        module, controller = self.bridge()
        module._known_catalog_capture_ids["c" * 64] = os.path.normcase(str(self.capture))
        module._known_catalog_identity_pairs.add(module._shader_restore_catalog_pairs(self.params())[0])
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", return_value=self.captured_dxil_evidence(module)), \
             mock.patch.object(module._shader_restore_protocol, "MAX_RECEIPT_BYTES", 1024):
            result = module._apply_shader_candidate_shared_resource(self.params())
        self.assertTrue(result["configured"])
        self.assertEqual(controller.replacements[77], 100)
        self.assertNotIn("restoreValidationReceipt", result)
        self.assertIn("exceeds", result["restoreValidationReceiptError"])

    def test_fast_path_rejects_changed_signed_bindings_before_build(self):
        receipt = self.fully_applied_receipt()
        changes = [
            ("expected_source_sha256", "1" * 64), ("expected_sha256", "2" * 64),
            ("expected_original_sha256", "3" * 64), ("abi_report_sha256", "4" * 64),
            ("abi_request_id", "5" * 64), ("expected_capture_session_id", "6" * 32),
            ("expected_capture_sha256", "0" * 64), ("revision", 2), ("entry_point", "other"),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                params = self.warm_params(copy.deepcopy(receipt))
                params[key] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    module._apply_shader_candidate_shared_resource(params)
                self.assertFalse(any(c[0] == "build" for c in controller.calls))
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        params = self.warm_params(copy.deepcopy(receipt))
        params["restore_validation_receipt"]["payload"]["physicalAbiSha256"] = "1" * 64
        with self.assertRaisesRegex(ValueError, "signature"):
            module._apply_shader_candidate_shared_resource(params)
        self.assertFalse(controller.calls)

    def test_changed_rdc_candidate_or_live_original_is_not_reused(self):
        receipt = self.fully_applied_receipt()
        for changed in ("rdc", "candidate", "live"):
            with self.subTest(changed=changed):
                self.capture.write_bytes(b"immutable captured frame")
                self.candidate.write_bytes(b"compiled candidate")
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                params = self.warm_params(receipt)
                if changed == "rdc":
                    self.capture.write_bytes(b"a different captured frame")
                elif changed == "candidate":
                    self.candidate.write_bytes(b"a different compiled candidate")
                else:
                    controller.original = b"another captured shader"
                with self.assertRaises((ValueError, RuntimeError)):
                    module._apply_shader_candidate_shared_resource(params)
                self.assertFalse(any(c[0] == "build" for c in controller.calls))

    def test_signed_pair_reuses_abi_across_workspace_metadata_and_package_changes(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        module._shader_restore_package_provenance_sha256 = "1" * 64
        params = dict(self.warm_params(receipt),
                      abi_validation_mode="verified-shader-pair",
                      workspace_id="2" * 32, revision=3,
                      expected_source_sha256="3" * 64,
                      abi_request_id="4" * 64, abi_report_sha256="5" * 64,
                      expected_profile="ps_6_0",
                      use_current_anchor_resource=True,
                      expected_original_resource_id=91,
                      expected_physical_abi_sha256=receipt["payload"]["physicalAbiSha256"])
        params["workspace_catalog_binding"]["projection"]["newAllocation"] = True
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", side_effect=AssertionError("no printer")), \
                mock.patch.object(module, "_physical_shader_interface_sha256", side_effect=AssertionError("no ABI repeat")), \
                mock.patch.object(module, "_dxil_ambiguous_interface_compatible", side_effect=AssertionError("no comparison repeat")):
            result = module._apply_shader_candidate_shared_resource(params)
        self.assertTrue(result["configured"])
        self.assertEqual(result["abiValidationMode"], "verified-shader-pair")
        self.assertFalse(result["freshPhysicalAbiValidationPerformed"])
        self.assertEqual(result["workspaceId"], params["workspace_id"])
        self.assertIn(("replace", 91, 100), controller.calls)
        self.assertNotIn(("reflect", 100), controller.calls)
        self.assertEqual([call for call in controller.calls if call[0] == "event"], [])
        rebound = module._shader_restore_receipt_store.verify(result["restoreValidationReceipt"])
        self.assertEqual(rebound["workspaceId"], params["workspace_id"])
        self.assertEqual(rebound["sourceSha256"], params["expected_source_sha256"])
        self.assertEqual(rebound["abiRequestId"], params["abi_request_id"])
        self.assertEqual(rebound["validationPolicySha256"], module._shader_restore_policy_sha256)
        self.assertEqual(rebound["physicalValidationBinding"], receipt["payload"]["physicalValidationBinding"])
        self.assertEqual(rebound["packageProvenanceSha256"], "1" * 64)
        self.assertEqual(rebound["targetProfile"], "ps_6_0")
        self.assertEqual(rebound["capturedDxilEvidence"], receipt["payload"]["capturedDxilEvidence"])
        exact, stable = module._shader_restore_catalog_pairs(params)
        self.assertNotIn(exact, module._known_catalog_identity_pairs)
        self.assertNotIn(stable, module._known_catalog_stable_pairs)

    def test_signed_pair_rejects_semantic_policy_and_physical_identity_changes(self):
        receipt = self.fully_applied_receipt()
        for changed in ("pair-policy", "physical-abi", "native-profile"):
            with self.subTest(changed=changed):
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                prior = copy.deepcopy(receipt)
                params = dict(self.warm_params(prior), abi_validation_mode="verified-shader-pair",
                              expected_physical_abi_sha256=receipt["payload"]["physicalAbiSha256"])
                if changed == "pair-policy":
                    prior["payload"]["pairValidationPolicyVersion"] = 2
                elif changed == "native-profile":
                    prior["payload"]["targetProfile"] = "ps_6_0"
                    params["expected_profile"] = "ps_6_1"
                else:
                    params["expected_physical_abi_sha256"] = "0" * 64
                params["restore_validation_receipt"] = module._shader_restore_receipt_store.seal(prior["payload"])
                with self.assertRaises(RuntimeError):
                    module._apply_shader_candidate_shared_resource(params)
                self.assertFalse(any(call[0] == "build" for call in controller.calls))

    def test_capture_hash_cache_rechecks_new_sessions_and_changed_file_facts(self):
        module, unused = self.bridge()
        cache = module._shader_restore_protocol.CaptureDigestCache()
        import builtins
        real_open = builtins.open
        with mock.patch("builtins.open", wraps=real_open) as opened:
            first = cache.digest(str(self.capture), "first")
            self.assertEqual(cache.digest(str(self.capture), "first"), first)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(cache.digest(str(self.capture), "second"), first)
            self.assertEqual(opened.call_count, 2)
            self.capture.write_bytes(b"changed frame")
            self.assertNotEqual(cache.digest(str(self.capture), "second"), first)
            self.assertEqual(opened.call_count, 3)

    @unittest.skipUnless(os.name == "nt", "Windows BCrypt provider")
    def test_windows_sha256_matches_reference_across_binary_and_read_boundaries(self):
        module, unused = self.bridge()
        protocol = module._shader_restore_protocol
        for data in (b"", b"abc", bytes(range(256)) * 3,
                     b"a\x00\xff\x80" * (1024 * 1024 + 17)):
            with self.subTest(byte_size=len(data)):
                native = protocol._BCryptFileHash()
                with mock.patch.object(protocol, "_file_hasher", return_value=native):
                    actual = protocol._stream_sha256(io.BytesIO(data))
                self.assertEqual(actual, hashlib.sha256(data).hexdigest())
                self.assertFalse(native.handle)
                self.assertFalse(native.provider)

    def test_stream_sha256_falls_back_before_read_if_native_provider_is_unavailable(self):
        module, unused = self.bridge()
        protocol = module._shader_restore_protocol
        data = b"fallback must hash the same complete byte stream\x00\xff"
        with mock.patch.object(protocol.os, "name", "nt"), mock.patch.object(
                protocol, "_BCryptFileHash", side_effect=OSError("provider unavailable")):
            self.assertEqual(protocol._stream_sha256(io.BytesIO(data)), hashlib.sha256(data).hexdigest())
        with mock.patch.object(protocol.os, "name", "posix"), mock.patch.object(
                protocol, "_BCryptFileHash", side_effect=AssertionError("Windows only")):
            self.assertEqual(protocol._stream_sha256(io.BytesIO(data)), hashlib.sha256(data).hexdigest())

    @unittest.skipUnless(os.name == "nt", "Windows BCrypt provider")
    def test_native_initialization_failure_closes_the_open_algorithm_provider(self):
        import ctypes
        module, unused = self.bridge()
        api = mock.Mock()

        def open_provider(provider, algorithm, implementation, flags):
            provider._obj.value = 123
            return 0

        def object_length(provider, property_name, size, capacity, returned, flags):
            size._obj.value = 256
            returned._obj.value = ctypes.sizeof(ctypes.c_ulong)
            return 0

        api.BCryptOpenAlgorithmProvider.side_effect = open_provider
        api.BCryptGetProperty.side_effect = object_length
        api.BCryptCreateHash.return_value = -1
        with mock.patch.object(ctypes, "WinDLL", return_value=api):
            with self.assertRaisesRegex(OSError, "Windows SHA256 failed"):
                module._shader_restore_protocol._BCryptFileHash()
        api.BCryptCloseAlgorithmProvider.assert_called_once()
        self.assertEqual(api.BCryptCloseAlgorithmProvider.call_args.args[0].value, 123)
        api.BCryptDestroyHash.assert_not_called()

    def test_hash_failure_releases_native_state_and_does_not_cache_partial_digest(self):
        module, unused = self.bridge()
        protocol = module._shader_restore_protocol
        for failing_step in ("update", "hexdigest"):
            with self.subTest(failing_step=failing_step):
                native = mock.Mock()
                getattr(native, failing_step).side_effect = OSError("hash operation failed")
                cache = protocol.CaptureDigestCache()
                with mock.patch.object(protocol, "_file_hasher", return_value=native):
                    with self.assertRaisesRegex(OSError, "hash operation failed"):
                        cache.digest(str(self.capture), "first")
                native.close.assert_called_once_with()
                self.assertIsNone(cache.key)
                self.assertIsNone(cache.value)
                self.assertEqual(cache.digest(str(self.capture), "first"),
                                 hashlib.sha256(self.capture.read_bytes()).hexdigest())

    def test_native_hash_still_rejects_file_changes_during_read(self):
        module, unused = self.bridge()
        protocol = module._shader_restore_protocol
        digest = hashlib.sha256()
        original_mtime = self.capture.stat().st_mtime_ns

        def update(data):
            digest.update(data)
            os.utime(self.capture, ns=(original_mtime, original_mtime + 1000000000))

        native = mock.Mock(update=update, hexdigest=digest.hexdigest)
        cache = protocol.CaptureDigestCache()
        with mock.patch.object(protocol, "_file_hasher", return_value=native):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                cache.digest(str(self.capture), "first")
        native.close.assert_called_once_with()
        self.assertIsNone(cache.key)
        self.assertIsNone(cache.value)

    def test_rejected_unknown_capture_ticket_can_prove_no_operation_before_cold_retry(self):
        module, controller = self.bridge(session="8" * 32)
        params = {"operation_id": "1" * 32, "expected_capture_id": "c" * 64,
                  "expected_capture_session_id": "8" * 32, "workspace_id": "b" * 32}
        result = module._recover_shader_candidate_shared_resource_operation(params)
        self.assertEqual(result["status"], "absent")
        self.assertTrue(result["strictReceipt"])
        self.assertFalse(result["mutationPresent"])
        self.assertFalse(controller.calls)
        self.assertFalse(module._known_catalog_capture_ids)
        with self.assertRaisesRegex(RuntimeError, "session"):
            module._recover_shader_candidate_shared_resource_operation(dict(params, expected_capture_session_id="7" * 32))
        module._shared_shader_operation_outcomes["1" * 32] = {"state": "committed"}
        with self.assertRaisesRegex(RuntimeError, "catalog identity"):
            module._recover_shader_candidate_shared_resource_operation(params)

    def test_valid_signed_receipt_is_invalid_after_validation_policy_change(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        self.assertEqual(receipt["payload"]["validationPolicySha256"], module._shader_restore_policy_sha256)
        module._shader_restore_policy_sha256 = "1" * 64
        with self.assertRaisesRegex(RuntimeError, "policy or replay binary changed"):
            module._apply_shader_candidate_shared_resource(self.warm_params(receipt))
        self.assertFalse(controller.calls)
        self.assertFalse(module._known_catalog_capture_ids)
        policy_file = self.root / "validation.py"
        policy_file.write_bytes(b"first validation logic")
        first = module._shader_restore_protocol.validation_policy_sha256([str(policy_file)])
        policy_file.write_bytes(b"changed validation logic")
        self.assertNotEqual(first, module._shader_restore_protocol.validation_policy_sha256([str(policy_file)]))

    def test_full_rdc_restore_uses_signed_physical_binding_after_presentation_update(self):
        receipt = self.fully_applied_receipt()
        module, controller = self.bridge(resource_id=91, session="8" * 32)
        module._shader_restore_package_provenance_sha256 = "1" * 64
        with mock.patch.object(module, "_physical_shader_interface_sha256", side_effect=AssertionError("no ABI repeat")), \
             mock.patch.object(module, "_dxil_evidence_from_live_disassembly", side_effect=AssertionError("no DXIL repeat")), \
             mock.patch.object(module, "_dxil_ambiguous_interface_compatible", side_effect=AssertionError("no comparison repeat")):
            result = module._apply_shader_candidate_shared_resource(self.warm_params(receipt))
        self.assertEqual(result["abiValidationMode"], "verified-rdc-restore")
        self.assertFalse(result["freshPhysicalAbiValidationPerformed"])
        self.assertEqual(result["restoreValidationReceipt"], receipt)
        self.assertIn(("replace", 91, 100), controller.calls)

    def test_authenticated_legacy_proof_requires_one_fresh_validation_not_false_reseal(self):
        receipt = self.fully_applied_receipt()
        for mode in ("verified-rdc-restore", "verified-shader-pair"):
            with self.subTest(mode=mode):
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                payload = copy.deepcopy(receipt["payload"])
                del payload["physicalValidationBinding"]
                legacy = module._shader_restore_receipt_store.seal(payload)
                params = dict(self.warm_params(legacy), abi_validation_mode=mode)
                with self.assertRaisesRegex(RuntimeError, "fresh validation is required once"):
                    module._apply_shader_candidate_shared_resource(params)
                self.assertFalse(controller.calls)

    def test_authenticated_physical_binding_change_or_unsigned_tamper_never_reuses_abi(self):
        receipt = self.fully_applied_receipt()
        for authenticated in (False, True):
            with self.subTest(authenticated=authenticated):
                module, controller = self.bridge(resource_id=91, session="8" * 32)
                changed = copy.deepcopy(receipt)
                changed["payload"]["physicalValidationBinding"]["abiRulesVersion"] += 1
                if authenticated:
                    changed = module._shader_restore_receipt_store.seal(changed["payload"])
                with self.assertRaises((ValueError, RuntimeError)):
                    module._apply_shader_candidate_shared_resource(self.warm_params(changed))
                self.assertFalse(controller.calls)

    def test_unavailable_semantic_policy_uses_exact_complete_policy_without_disabling_bridge(self):
        module, unused = self.bridge(resource_id=91, session="8" * 32)
        module._shader_restore_physical_validation_binding = None
        module._shader_restore_policy_sha256 = module._shader_restore_package_provenance_sha256
        self.assertNotEqual(module._shader_restore_policy_sha256, "")
        payload = self.fully_applied_receipt()["payload"]
        payload["physicalValidationBinding"] = None
        payload["validationPolicySha256"] = module._shader_restore_policy_sha256
        receipt = module._shader_restore_receipt_store.seal(payload)
        params = self.warm_params(receipt)
        self.assertEqual(module._verified_shader_restore_request(params)["candidateRawSha256"], payload["candidateRawSha256"])
        payload["validationPolicySha256"] = "1" * 64
        params["restore_validation_receipt"] = module._shader_restore_receipt_store.seal(payload)
        with self.assertRaisesRegex(RuntimeError, "conservative validation policy"):
            module._verified_shader_restore_request(params)

    def test_existing_capture_metadata_exposes_loaded_policy_without_hashing(self):
        module, controller = self.bridge()
        controller.GetAPIProperties = lambda: types.SimpleNamespace(
            pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12")
        controller.GetFrameInfo = lambda: types.SimpleNamespace(frameNumber=1)
        module._ctx.APIProps = controller.GetAPIProperties
        module._ctx.IsCaptureLocal = lambda: True
        with mock.patch.object(module._shader_restore_capture_digests, "digest",
                               side_effect=AssertionError("metadata must not hash RDC")), \
             mock.patch.object(module._shader_restore_protocol, "validation_policy_sha256",
                               side_effect=AssertionError("metadata must not rehash policy")):
            status = module._get_capture_status({})
            context = module._get_current_renderdoc_context({"identity_only": True})
        self.assertEqual(status["restoreValidationPolicySha256"], module._shader_restore_policy_sha256)
        self.assertEqual(context["restoreValidationPolicySha256"], module._shader_restore_policy_sha256)
        self.assertEqual(context["capture"]["captureSessionId"], status["captureSessionId"])
        self.assertTrue(context["identityOnly"])
        self.assertEqual(context, module._ai_context_protocol.validate_context(context))
        context["restoreValidationPolicySha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "content hash does not match"):
            module._ai_context_protocol.validate_context(context)

    def test_catalog_remains_captured_original_after_checked_shared_replacement(self):
        module, controller = self.bridge()
        module._active_capture_token = module._current_capture_token()
        controller.GetAPIProperties = lambda: types.SimpleNamespace(
            pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12")
        controller.GetFrameInfo = lambda: types.SimpleNamespace(frameNumber=1)
        get_shader = controller.GetShader

        def simple_reflection(*arguments):
            value = get_shader(*arguments)
            value.constantBlocks = []
            return value

        controller.GetShader = simple_reflection
        before = module._export_shader_catalog({
            "output_dir": str(self.root / "before"), "event_ids": [321], "stages": ["fragment"]})
        module._known_catalog_capture_ids["c" * 64] = os.path.normcase(str(self.capture))
        with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", return_value=(None, {})), \
             mock.patch.object(module, "_physical_shader_interface_sha256", return_value="9" * 64), \
             mock.patch.object(module, "_dxil_ambiguous_interface_compatible", return_value=True):
            applied = module._apply_shader_candidate_shared_resource(self.params())
        self.assertTrue(applied["configured"])
        self.assertEqual(controller.replacements[77], 100)
        owner = module._shared_shader_replacements[77]
        after = module._export_shader_catalog({
            "output_dir": str(self.root / "after"), "event_ids": [321], "stages": ["fragment"],
            "expected_stable_capture_id": before["stableCaptureId"],
            "expected_stable_identity_evidence_sha256": before["stableIdentityEvidenceSha256"]})
        for field in ("captureId", "stableCaptureId", "identityEvidenceSha256", "stableIdentityEvidenceSha256"):
            self.assertEqual(after[field], before[field])
        catalog = json.loads(Path(after["catalogPath"]).read_text(encoding="utf-8"))
        self.assertEqual(catalog["errors"], [])
        self.assertEqual(catalog["shaders"][0]["rawSha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(controller.replacements[77], 100)
        self.assertIs(module._shared_shader_replacements[77], owner)

        for changed in ("session", "capture", "original-id", "original-bytes", "active-bytes", "stage", "api"):
            saved_owner = dict(owner)
            saved_target = controller.targets[100]
            if changed == "session": owner["captureSessionId"] = "e" * 32
            elif changed == "capture": owner["captureToken"] = ("loaded", "other.rdc")
            elif changed == "original-id": owner["expectedOriginalResourceId"] = 91
            elif changed == "original-bytes": owner["capturedOriginalReflection"] = reflection(b"stale original")
            elif changed == "active-bytes": controller.targets[100] = b"foreign target"
            elif changed == "stage": owner["stageName"] = "vertex"
            else: owner["api"] = "d3d11"
            with self.subTest(changed=changed):
                result = module._export_shader_catalog({
                    "output_dir": str(self.root / changed), "event_ids": [321], "stages": ["fragment"]})
                rejected = json.loads(Path(result["catalogPath"]).read_text(encoding="utf-8"))
                self.assertEqual(rejected["shaders"], [])
                self.assertTrue(rejected["errors"])
                self.assertEqual(controller.replacements[77], 100)
            owner.clear()
            owner.update(saved_owner)
            controller.targets[100] = saved_target


if __name__ == "__main__":
    unittest.main()
