import base64
import enum
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from src.rdc_project_index import new_index


REPOSITORY = Path(__file__).resolve().parents[2]


class _Properties:
    def __init__(self):
        self.name = ""
        self.type = None
        self.version = 0
        self.flags = None
        self.uncompressedSize = 0


class _WriteResult:
    message = ""

    @staticmethod
    def OK():
        return True


class _CaptureAccess:
    def __init__(self):
        self.sections = []

    def GetSectionCount(self):
        return len(self.sections)

    def GetSectionProperties(self, index):
        return self.sections[index][0]

    def GetSectionContents(self, index):
        return self.sections[index][1]

    def WriteSection(self, properties, contents):
        properties.uncompressedSize = len(contents)
        record = (properties, bytes(contents))
        for index, (current, unused) in enumerate(self.sections):
            del unused
            if current.name == properties.name:
                self.sections[index] = record
                break
        else:
            self.sections.append(record)
        return _WriteResult()


class _Pipeline:
    def __init__(self, resource_id):
        self.resource_id = resource_id

    def GetShader(self, unused_stage):
        del unused_stage
        return self.resource_id

    @staticmethod
    def GetShaderPipelineObject():
        return 99


class _Controller:
    def __init__(self, raw, *, api="D3D11", encoding="DXBC", blocks=None):
        self.raw = raw
        self.api = api
        self.encoding = encoding
        self.events = []
        self.entry = types.SimpleNamespace(name="main", stage="fragment")
        self.resource = types.SimpleNamespace(type="shader", resourceId=7)
        self.resources = [self.resource]
        self.reflection = types.SimpleNamespace(
            entryPoint="main",
            stage="fragment",
            encoding=encoding,
            rawBytes=self.raw,
            inputSignature=[],
            outputSignature=[],
            readOnlyResources=[],
            readWriteResources=[],
            samplers=[],
            constantBlocks=list(blocks or []),
            interfaces=[],
            outputTopology="",
            dispatchThreadsDimension=[],
        )

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))

    @staticmethod
    def GetPipelineState():
        return _Pipeline(7)

    def GetAPIProperties(self):
        return types.SimpleNamespace(pipelineType=self.api)

    def GetResources(self):
        return list(self.resources)

    def GetShaderEntryPoints(self, unused_resource):
        del unused_resource
        return [self.entry]

    def GetShader(self, unused_pipeline, unused_resource, unused_entry):
        del unused_pipeline, unused_resource, unused_entry
        return self.reflection


def _constant_block(name="PerMaterial", bind=0, space=0, byte_size=16):
    return types.SimpleNamespace(
        name=name,
        fixedBindNumber=bind,
        fixedBindSetOrSpace=space,
        bindArraySize=1,
        byteSize=byte_size,
        bufferBacked=True,
        inlineDataBytes=False,
        compileConstants=False,
        variables=[],
    )


class _Replay:
    def __init__(self, access, controller):
        self.access = access
        self.controller = controller
        self.async_tags = []
        self._async_queue = []
        self._draining_async = False

    def GetCaptureAccess(self):
        return self.access

    def GetCaptureFile(self):
        return self.access

    def BlockInvoke(self, callback):
        callback(self.controller)

    def AsyncInvoke(self, method, tag=""):
        assert callable(method) and isinstance(tag, str)
        self.async_tags.append(tag)
        self._async_queue.append(method)
        if self._draining_async:
            return
        self._draining_async = True
        try:
            while self._async_queue:
                self._async_queue.pop(0)(self.controller)
        finally:
            self._draining_async = False


class _Context:
    def __init__(self, path, replay):
        self.path = str(path)
        self.replay = replay

    @staticmethod
    def IsCaptureLoaded():
        return True

    def GetCaptureFilename(self):
        return self.path

    def Replay(self):
        return self.replay

    @staticmethod
    def CurEvent():
        return 5


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.SectionProperties = _Properties
    fake_renderdoc.SectionType = types.SimpleNamespace(Unknown="unknown")
    fake_renderdoc.SectionFlags = types.SimpleNamespace(
        ZstdCompressed="zstd"
    )
    fake_renderdoc.ShaderStage = types.SimpleNamespace(Fragment="fragment")
    fake_renderdoc.ResourceType = types.SimpleNamespace(Shader="shader")
    fake_renderdoc.ResourceId = lambda: 0
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_rdc_project_test", path
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # This fixture models the replay API, not native Qt/qrenderdoc. A
        # separately installed PySide2 must not enable the real UI integration.
        with mock.patch.dict(sys.modules, {"PySide2": None}):
            spec.loader.exec_module(module)
        return module
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc


class ReplayLabRdcProjectBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.capture_path = Path(self.temporary.name) / "frame.rdc"
        self.capture_path.write_bytes(b"capture")
        self.access = _CaptureAccess()
        self.controller = _Controller(
            b"captured-shader", blocks=[_constant_block()]
        )
        replay = _Replay(self.access, self.controller)
        self.bridge._ctx = _Context(self.capture_path, replay)
        self.bridge._capture_session_id = "1" * 32
        self.bridge._known_catalog_capture_ids = {}
        self.bridge._known_catalog_identity_pairs = set()
        self.bridge._symbol_capture_snapshot = {
            "captureId": "",
            "capturePath": "",
            "shaderKeys": set(),
            "usesByKey": {},
        }
        self.bridge._symbol_alias_overlay = self.bridge._new_symbol_alias_overlay()
        self.bridge._symbol_resource_renames = {}
        self.bridge._symbol_rename_capture_token = None
        self.bridge._symbol_alias_recovery_ledger = None
        self.bridge._symbol_alias_last_rollback_receipt = None
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()

    def tearDown(self):
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        self.bridge._ctx = None
        self.temporary.cleanup()

    def test_project_writer_uses_canonical_task_and_raygen_stage_names(self):
        self.assertEqual(
            self.bridge._normalise_stage_name("amplification"), "task"
        )
        self.assertEqual(
            self.bridge._normalise_stage_name("raygeneration"), "raygen"
        )
        self.assertEqual(
            self.bridge._normalise_stage_name("ray generation"), "raygen"
        )

        class FalseyVertexStage(int):
            def __new__(cls):
                return int.__new__(cls, 0)

            def __str__(self):
                return "ShaderStage.Vertex"

        vertex = FalseyVertexStage()
        self.assertFalse(bool(vertex))
        self.assertEqual(
            self.bridge._normalise_stage_name(vertex), "vertex"
        )

        class ModernShaderStage(enum.IntEnum):
            Vertex = 0

        self.assertEqual(
            self.bridge._normalise_stage_name(ModernShaderStage.Vertex),
            "vertex",
        )

    def _expected_shader(self):
        reflection = self.controller.reflection
        compiled = self.bridge._compiled_shader_interface(reflection)
        compiled["stage"] = self.bridge._normalise_stage_name(reflection.stage)
        compiled["entryPoint"] = reflection.entryPoint
        compiled["encoding"] = self.bridge._project_shader_index_encoding(
            reflection.encoding
        )
        interface_sha, layout_sha = self.bridge._project_symbol_hashes(compiled)
        blocks = [
            self.bridge._project_shader_index_block(
                block, index, compiled["stage"]
            )
            for index, block in enumerate(reflection.constantBlocks)
        ]
        return {
            "resourceId": 7,
            "stage": compiled["stage"],
            "entryPoint": reflection.entryPoint,
            "encoding": compiled["encoding"],
            "rawSha256": hashlib.sha256(reflection.rawBytes).hexdigest(),
            "interfaceHash": interface_sha,
            "layoutHash": layout_sha,
            "interfaceComplete": bool(compiled["interfaceComplete"]),
            "constantBlocks": blocks,
            "anchorEventId": 10,
        }

    def _index_request(self, shader=None):
        return {
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "expected_capture_id": "2" * 64,
            "expected_identity_evidence_sha256": "3" * 64,
            "expected_api": self.controller.api,
            "expected_shaders": [shader or self._expected_shader()],
        }

    def _inventory_request(self, *, expected_shaders=None, discover_anchors=True):
        request = self._index_request()
        request["inventory_all_shaders"] = True
        request["discover_anchor_eids"] = discover_anchors
        request["expected_shaders"] = list(expected_shaders or [])
        return request

    def _begin(self, shader=None):
        return self.bridge._begin_rdc_project_shader_resource_index(
            self._index_request(shader)
        )

    def _target_request(self, targets):
        request = self._inventory_request()
        request["target_raw_sha256s"] = targets
        return request

    def _replacement_owner(self, *, api="d3d12"):
        self.controller.api = "D3D12" if api == "d3d12" else "D3D11"
        self.controller.reflection.encoding = "DXIL" if api == "d3d12" else "DXBC"
        original = self.controller.reflection
        active = types.SimpleNamespace(**vars(original))
        active.rawBytes = b"different-active-replacement"
        owner = {
            "captureSessionId": self.bridge._capture_session_id,
            "captureToken": self.bridge._current_capture_token(), "api": api,
            "originalResource": 7, "expectedOriginalResourceId": 7,
            "expectedOriginalSha256": hashlib.sha256(original.rawBytes).hexdigest(),
            "stageName": "fragment", "entryPoint": "main",
            "candidateRawSha256": hashlib.sha256(active.rawBytes).hexdigest(),
            "capturedOriginalReflection": original, "capturedDxilLayoutEvidence": {},
        }
        self.controller.GetShader = lambda *args: active
        return original, active, owner

    def test_replaced_inventory_reuses_captured_identity_before_target_filter(self):
        original, active, owner = self._replacement_owner()
        raw = hashlib.sha256(original.rawBytes).hexdigest()
        with mock.patch.dict(self.bridge._shared_shader_replacements, {7: owner}, clear=True), \
                mock.patch.object(self.bridge, "_shared_shader_replacement_capture_token", owner["captureToken"]), \
                mock.patch.object(self.bridge, "_dxil_evidence_from_live_disassembly",
                                  side_effect=AssertionError("must not disassemble replacement")):
            result = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))["result"]
        self.assertEqual(result["originalShaderIdentityPolicy"], "captured-original-owner-v1")
        self.assertEqual(result["observedTargetRawSha256s"], [raw])
        self.assertEqual(result["targetMatchedShaderResourceIds"], [7])
        self.assertEqual(result["filteredOutShaderResourceIds"], [])
        self.assertTrue(result["rawIdentitySearchComplete"])
        self.assertEqual(result["assets"][0]["rawSha256"], raw)
        self.assertEqual(base64.b64decode(result["rawBytecodeBlobs"][0]["data"]), original.rawBytes)
        self.assertEqual(self.controller.events, [])
        self.assertIs(self.controller.GetShader(), active)

    def test_expected_inventory_also_matches_owned_original_not_candidate(self):
        original, unused_active, owner = self._replacement_owner(api="d3d11")
        expected = self._expected_shader()
        with mock.patch.dict(self.bridge._shared_shader_replacements, {7: owner}, clear=True), \
                mock.patch.object(self.bridge, "_shared_shader_replacement_capture_token", owner["captureToken"]):
            result = self._begin(expected)["result"]
        self.assertTrue(result["matched"], result)
        self.assertEqual(result["assets"][0]["rawSha256"], hashlib.sha256(original.rawBytes).hexdigest())
        self.assertEqual(self.controller.events, [])

    def test_replaced_inventory_refuses_stale_ownership_and_changed_identity(self):
        original, active, pristine = self._replacement_owner()
        raw = hashlib.sha256(original.rawBytes).hexdigest()
        variants = [("captureSessionId", "9" * 32), ("captureToken", ("loaded", "another.rdc")),
                    ("api", "d3d11"), ("originalResource", 8), ("expectedOriginalResourceId", 8),
                    ("stageName", "vertex"), ("entryPoint", "other"),
                    ("candidateRawSha256", "a" * 64), ("expectedOriginalSha256", "a" * 64),
                    ("capturedOriginalReflection", None), ("shared-token", None)]
        for field, value in variants:
            with self.subTest(field=field):
                self.bridge._project_shader_index_jobs.clear()
                owner = dict(pristine)
                shared_token = owner["captureToken"]
                if field == "shared-token":
                    shared_token = value
                else:
                    owner[field] = value
                with mock.patch.dict(self.bridge._shared_shader_replacements, {7: owner}, clear=True), \
                        mock.patch.object(self.bridge, "_shared_shader_replacement_capture_token", shared_token):
                    result = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))["result"]
                self.assertEqual(result["assets"], [])
                self.assertFalse(result["rawIdentitySearchComplete"])
                self.assertEqual(result["rawIdentityUnavailableShaderResourceIds"], [7])
                self.assertNotIn(7, result["filteredOutShaderResourceIds"])
                self.assertTrue(any(row["code"] == "shader-replacement-original-unavailable"
                                    for row in result["diagnostics"]))
        self.assertEqual(self.controller.events, [])

    def test_owned_dxil_missing_original_layout_never_disassembles_candidate(self):
        original, unused_active, owner = self._replacement_owner()
        owner.pop("capturedDxilLayoutEvidence")
        raw = hashlib.sha256(original.rawBytes).hexdigest()
        with mock.patch.dict(self.bridge._shared_shader_replacements, {7: owner}, clear=True), \
                mock.patch.object(self.bridge, "_shared_shader_replacement_capture_token", owner["captureToken"]), \
                mock.patch.object(self.bridge, "_dxil_evidence_from_live_disassembly") as disassemble:
            result = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))["result"]
        disassemble.assert_not_called()
        self.assertEqual(result["assets"][0]["rawSha256"], raw)
        self.assertFalse(result["assets"][0]["interfaceComplete"])
        self.assertEqual(self.controller.events, [])

    def test_target_raw_filter_is_strict_bounded_and_part_of_request_identity(self):
        raw = hashlib.sha256(self.controller.raw).hexdigest()
        request = self._target_request([raw])
        selected = self.bridge._validated_project_shader_index_request(request, str(self.capture_path))
        full = self.bridge._validated_project_shader_index_request(self._inventory_request(), str(self.capture_path))
        self.assertNotEqual(selected["requestSha256"], full["requestSha256"])
        first = self.bridge._validated_project_shader_index_request(self._target_request([raw, "a" * 64]), str(self.capture_path))
        reordered = self.bridge._validated_project_shader_index_request(self._target_request(["a" * 64, raw, raw]), str(self.capture_path))
        self.assertEqual(first["requestSha256"], reordered["requestSha256"])
        for targets in ([], [raw.upper()], [" " + raw], [None], raw, ["a" * 64] * 17):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.bridge._validated_project_shader_index_request(self._target_request(targets), str(self.capture_path))
        request["expected_shaders"] = [self._expected_shader()]
        with self.assertRaises(ValueError):
            self.bridge._validated_project_shader_index_request(request, str(self.capture_path))
        request = self._target_request([raw])
        request["inventory_all_shaders"] = False
        with self.assertRaises(ValueError):
            self.bridge._validated_project_shader_index_request(request, str(self.capture_path))
        first = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))
        other = self.bridge._begin_rdc_project_shader_resource_index(self._target_request(["a" * 64]))
        repeated = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw, raw]))
        self.assertNotEqual(first["jobId"], other["jobId"])
        self.assertEqual(first["jobId"], repeated["jobId"])
        self.assertTrue(repeated["idempotent"])

    def test_target_raw_filter_skips_nonmatching_dxil_abi_and_usage(self):
        self.controller.api = "D3D12"
        self.controller.reflection.encoding = "DXIL"
        self.controller.resources.append(types.SimpleNamespace(type="shader", resourceId=8))
        other = types.SimpleNamespace(**vars(self.controller.reflection))
        other.rawBytes = b"different-module"
        self.controller.GetShader = lambda pipeline, resource, entry: self.controller.reflection if int(resource) == 7 else other
        usage_calls = []
        self.controller.GetUsage = lambda resource: (usage_calls.append(int(resource)) or [types.SimpleNamespace(eventId=42)])
        raw = hashlib.sha256(self.controller.raw).hexdigest()
        analyzed = []

        def disassemble(controller, pipeline, reflection):
            analyzed.append(reflection.rawBytes)
            raise RuntimeError("fixture has no physical DXIL disassembler")

        original_snapshot = self.bridge._symbol_capture_snapshot
        with mock.patch.object(self.bridge, "_dxil_evidence_from_live_disassembly", side_effect=disassemble):
            snapshot = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))
        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertEqual(analyzed, [self.controller.raw])
        self.assertEqual(usage_calls, [7])
        self.assertEqual(result["inventoryScope"], "target-raw-sha256")
        self.assertFalse(result["inventoryComplete"])
        self.assertTrue(result["scanComplete"])
        self.assertTrue(result["targetInventoryComplete"])
        self.assertTrue(result["rawIdentitySearchComplete"])
        self.assertEqual(result["filteredOutShaderResourceIds"], [8])
        self.assertEqual(result["unindexedShaderResourceCount"], 0)
        self.assertEqual(result["targetMatchedShaderResourceIds"], [7])
        self.assertEqual(result["observedTargetRawSha256s"], [raw])
        self.assertEqual(result["assets"][0]["anchorEventId"], 42)
        self.assertEqual(result["assets"][0]["ownerCaptureSessionId"], "1" * 32)
        self.assertFalse(result["symbolSnapshotPublished"])
        self.assertEqual(result["symbolSnapshotShaderCount"], 0)
        self.assertIs(self.bridge._symbol_capture_snapshot, original_snapshot)
        self.assertEqual(self.bridge._known_catalog_capture_ids, {})
        self.assertEqual(self.controller.events, [])
        with self.assertRaises((RuntimeError, ValueError)):
            self._commit(snapshot)

    def test_target_raw_lookup_cannot_enter_full_inventory_cache_or_project_projection(self):
        from src import project_restore
        from src.rdc_shader_inventory_cache import build_shader_inventory_cache_document, RdcShaderInventoryCacheError
        raw = hashlib.sha256(self.controller.raw).hexdigest()
        result, unused_calls = self._host_target_inventory([raw])
        with self.assertRaisesRegex(RuntimeError, "filtered Shader lookup"):
            project_restore._project_shader_inventory(result)
        with self.assertRaises(RdcShaderInventoryCacheError):
            build_shader_inventory_cache_document("1" * 32, "2" * 64, self.capture_path, result["bridgeResult"])

    def test_target_raw_invalid_physical_resource_never_completes_search(self):
        self.controller.resources.append(types.SimpleNamespace(type="shader", resourceId=0))
        snapshot = self.bridge._begin_rdc_project_shader_resource_index(self._target_request(["a" * 64]))
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["result"]["reasonId"], "shader-resource-index-invalid-resource-id")
        self.assertFalse(snapshot["result"]["scanComplete"])
        self.assertFalse(snapshot["result"]["rawIdentitySearchComplete"])

    def test_target_raw_unobserved_is_not_an_absence_claim_or_full_inventory(self):
        requested = "a" * 64
        request = self._target_request([requested])
        with mock.patch.object(self.bridge, "_compiled_shader_interface", side_effect=AssertionError("nonmatch ABI work")):
            result = self.bridge._begin_rdc_project_shader_resource_index(request)["result"]
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["observedTargetRawSha256s"], [])
        self.assertEqual(result["unobservedTargetRawSha256s"], [requested])
        self.assertEqual(result["filteredOutShaderResourceCount"], 1)
        self.assertTrue(result["rawIdentitySearchComplete"])
        self.assertTrue(result["targetInventoryComplete"])
        self.assertFalse(result["inventoryComplete"])
        self.assertFalse(result["physicalApplyAuthority"])
        self.assertNotIn("absentTargetRawSha256s", result)
        self.assertEqual(result["rawHashPurpose"], "captured-executable-identity")
        self.assertFalse(result["semanticEquivalenceVerified"])

    def test_target_raw_lookup_preserves_unreadable_resources_and_partial_target_metadata(self):
        self.controller.resources.append(types.SimpleNamespace(type="shader", resourceId=8))
        original = self.controller.reflection
        self.controller.GetShader = lambda pipeline, resource, entry: original if int(resource) == 7 else None
        result = self.bridge._begin_rdc_project_shader_resource_index(self._target_request(["a" * 64]))["result"]
        self.assertEqual(result["filteredOutShaderResourceIds"], [7])
        self.assertEqual(result["rawIdentityUnavailableShaderResourceIds"], [8])
        self.assertEqual(result["unindexedShaderResourceIds"], [8])
        self.assertFalse(result["rawIdentitySearchComplete"])
        self.assertFalse(result["targetInventoryComplete"])
        self.assertEqual(result["unobservedTargetRawSha256s"], ["a" * 64])
        # A raw match with contradictory entry-point metadata is observed,
        # but does not become a usable source/anchor tuple.
        original.entryPoint = "contradictory-entry"
        raw = hashlib.sha256(original.rawBytes).hexdigest()
        result = self.bridge._begin_rdc_project_shader_resource_index(self._target_request([raw]))["result"]
        self.assertEqual(result["observedTargetRawSha256s"], [raw])
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["targetMatchedShaderResourceIds"], [7])
        self.assertFalse(result["targetInventoryComplete"])

    def _host_target_inventory(self, targets, mutate=None):
        from src import project_restore
        bridge = self.bridge
        calls = []

        class HostIpc:
            def call(self, command, params, timeout):
                calls.append((command, params))
                if command == "begin_rdc_project_shader_resource_index":
                    result = bridge._begin_rdc_project_shader_resource_index(params)
                elif command == "poll_rdc_project_shader_resource_index":
                    result = bridge._poll_rdc_project_shader_resource_index(params)
                else:
                    raise AssertionError("unexpected Host command " + command)
                if mutate and result.get("status") == "completed":
                    result = json.loads(json.dumps(result))
                    mutate(result["result"])
                return result

        host = types.SimpleNamespace(ipc=HostIpc())
        value = project_restore._inventory_shader_resources(
            host, self.capture_path, "1" * 32, self.controller.api,
            project_restore._Progress("test-target", None, []), target_raw_sha256s=targets)
        return value, calls

    def test_host_target_inventory_uses_real_byte_hash_anchor_and_retains_filtered_scope(self):
        self.controller.resources.append(types.SimpleNamespace(type="shader", resourceId=8))
        other = types.SimpleNamespace(**vars(self.controller.reflection))
        other.rawBytes = b"other-shader"
        self.controller.GetShader = lambda pipeline, resource, entry: self.controller.reflection if int(resource) == 7 else other
        self.controller.GetUsage = lambda resource: [types.SimpleNamespace(eventId=42)]
        raw = hashlib.sha256(self.controller.raw).hexdigest()
        result, calls = self._host_target_inventory([raw])
        self.assertEqual(result["shaders"][0]["resourceId"], 7)
        self.assertEqual(result["shaders"][0]["rawSha256"], raw)
        self.assertEqual(result["shaders"][0]["anchorEventId"], 42)
        self.assertEqual(result["rawBytecode"][raw], self.controller.raw)
        self.assertEqual(result["inventoryScope"], "target-raw-sha256")
        self.assertEqual(result["filteredOutShaderResourceCount"], 1)
        self.assertEqual(calls[0][1]["target_raw_sha256s"], [raw])
        self.assertEqual(calls[0][1]["expected_shaders"], [])
        self.assertFalse(result["bridgeResult"]["inventoryComplete"])
        self.assertFalse(result["bridgeResult"]["symbolSnapshotPublished"])
        self.assertEqual(self.controller.events, [])

    def test_host_target_inventory_returns_unobserved_with_explicit_incomplete_raw_search(self):
        self.controller.GetShader = lambda pipeline, resource, entry: None
        result, unused_calls = self._host_target_inventory(["a" * 64])
        self.assertEqual(result["shaders"], [])
        self.assertEqual(result["unobservedTargetRawSha256s"], ["a" * 64])
        self.assertFalse(result["rawIdentitySearchComplete"])
        self.assertFalse(result["targetInventoryComplete"])

    def test_host_target_inventory_rejects_widened_scope_ledger_and_claimed_publication(self):
        from src import project_restore
        raw = hashlib.sha256(self.controller.raw).hexdigest()
        corruptions = (
            ("inventoryComplete", True), ("symbolSnapshotPublished", True),
            ("targetRawSha256s", ["a" * 64]), ("rawIdentitySearchComplete", False),
            ("filteredOutShaderResourceIds", [7]), ("filteredOutShaderResourceCount", 1),
            ("targetMatchedShaderResourceIds", []), ("observedTargetRawSha256s", []),
            ("symbolSnapshotShaderCount", 1), ("scanComplete", False),
            ("targetInventoryComplete", False),
            ("rawHashPurpose", "organized-code-equivalence"), ("semanticEquivalenceVerified", True),
        )
        for field, value in corruptions:
            with self.subTest(field=field), self.assertRaises(project_restore.CatalogPreparationRequired):
                self._host_target_inventory([raw], lambda result, key=field, invalid=value: result.update({key: invalid}))

    def test_host_target_inventory_rejects_invalid_filter_before_ipc(self):
        from src import project_restore
        ipc = types.SimpleNamespace(call=mock.Mock(side_effect=AssertionError("IPC must not run")))
        for targets in ([], "a" * 64, ["A" * 64], [" a" * 32], [True], ["a" * 64] * 17):
            with self.subTest(targets=targets), self.assertRaises((ValueError, RuntimeError)):
                project_restore._inventory_shader_resources(
                    types.SimpleNamespace(ipc=ipc), self.capture_path, "1" * 32, self.controller.api,
                    project_restore._Progress("invalid-target", None, []), target_raw_sha256s=targets)
        ipc.call.assert_not_called()

    def test_target_raw_missing_bytes_or_entry_points_remain_unknown(self):
        for unavailable in ("raw", "entries-empty", "entries-error"):
            with self.subTest(unavailable=unavailable):
                with self.bridge._project_shader_index_jobs_guard:
                    self.bridge._project_shader_index_jobs.clear()
                self.controller.reflection.rawBytes = self.controller.raw
                self.controller.GetShaderEntryPoints = lambda resource: [self.controller.entry]
                if unavailable == "raw":
                    self.controller.reflection.rawBytes = b""
                elif unavailable == "entries-empty":
                    self.controller.GetShaderEntryPoints = lambda resource: []
                else:
                    self.controller.GetShaderEntryPoints = mock.Mock(side_effect=RuntimeError("driver unavailable"))
                result, unused_calls = self._host_target_inventory(["a" * 64])
                self.assertFalse(result["rawIdentitySearchComplete"])
                self.assertFalse(result["targetInventoryComplete"])
                self.assertEqual(result["rawIdentityUnavailableShaderResourceIds"], [7])
                self.assertEqual(result["filteredOutShaderResourceCount"], 0)
                self.assertEqual(result["unobservedTargetRawSha256s"], ["a" * 64])

    def _commit(self, snapshot):
        return self.bridge._commit_rdc_project_shader_resource_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "job_id": snapshot["jobId"],
            "expected_capture_id": "2" * 64,
            "expected_identity_evidence_sha256": "3" * 64,
            "live_identity_sha256": snapshot["result"]["liveIdentitySha256"],
        })

    def test_named_unknown_section_round_trips_exact_sealed_document(self):
        missing = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })
        self.assertFalse(missing["present"])

        document = new_index()
        written = self.bridge._write_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "document": document,
        })
        self.assertTrue(written["written"])
        self.assertFalse(written["physicalApplyAuthority"])
        self.assertEqual(document, written["document"])
        self.assertEqual("unknown", self.access.sections[0][0].type)
        self.assertEqual("zstd", self.access.sections[0][0].flags)
        self.assertEqual(document["schemaVersion"], self.access.sections[0][0].version)

        read = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })
        self.assertEqual(document, read["document"])
        self.assertEqual(written["encodedSha256"], read["encodedSha256"])

    def test_legacy_v1_section_remains_readable_without_transport_migration(self):
        unsigned = {
            "schema": "replaylab-rdc-project-reuse-index/v1",
            "schemaVersion": 1,
            "bindings": [],
        }
        document = dict(unsigned)
        document["sealSha256"] = hashlib.sha256(
            self.bridge._canonical_project_index_bytes(unsigned)
        ).hexdigest()
        payload = self.bridge._canonical_project_index_bytes(document)
        properties = _Properties()
        properties.name = self.bridge.REPLAYLAB_RDC_PROJECT_INDEX_SECTION
        properties.version = 1
        properties.uncompressedSize = len(payload)
        self.access.sections = [(properties, payload)]

        result = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })

        self.assertTrue(result["present"])
        self.assertEqual(result["sectionVersion"], 1)
        self.assertEqual(result["document"], document)

    def test_v2_section_remains_readable_but_bridge_only_writes_v3(self):
        unsigned = {
            "schema": "replaylab-rdc-project-reuse-index/v2",
            "schemaVersion": 2,
            "bindings": [],
        }
        document = dict(unsigned)
        document["sealSha256"] = hashlib.sha256(
            self.bridge._canonical_project_index_bytes(unsigned)
        ).hexdigest()
        payload = self.bridge._canonical_project_index_bytes(document)
        properties = _Properties()
        properties.name = self.bridge.REPLAYLAB_RDC_PROJECT_INDEX_SECTION
        properties.version = 2
        properties.uncompressedSize = len(payload)
        self.access.sections = [(properties, payload)]

        result = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })
        self.assertTrue(result["present"])
        self.assertEqual(result["sectionVersion"], 2)
        self.assertEqual(result["document"], document)

        with self.assertRaisesRegex(ValueError, "only writes the current"):
            self.bridge._write_rdc_project_index({
                "expected_capture_path": str(self.capture_path),
                "expected_capture_session_id": "1" * 32,
                "document": document,
            })

    def test_section_and_document_versions_must_match_for_v3(self):
        document = new_index()
        payload = self.bridge._canonical_project_index_bytes(document)
        properties = _Properties()
        properties.name = self.bridge.REPLAYLAB_RDC_PROJECT_INDEX_SECTION
        properties.version = 2
        properties.uncompressedSize = len(payload)
        self.access.sections = [(properties, payload)]

        with self.assertRaisesRegex(RuntimeError, "versions differ"):
            self.bridge._read_rdc_project_index({
                "expected_capture_path": str(self.capture_path),
                "expected_capture_session_id": "1" * 32,
            })

    def test_duplicate_named_sections_and_non_finite_json_fail_closed(self):
        document = new_index()
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        for unused in range(2):
            del unused
            properties = _Properties()
            properties.name = self.bridge.REPLAYLAB_RDC_PROJECT_INDEX_SECTION
            properties.version = 1
            properties.uncompressedSize = len(payload)
            self.access.sections.append((properties, payload))
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.bridge._read_rdc_project_index({})

        self.access.sections.pop()
        invalid = payload.replace(b'"bindings":[]', b'"bindings":[],"x":NaN')
        self.access.sections[0] = (self.access.sections[0][0], invalid)
        self.access.sections[0][0].uncompressedSize = len(invalid)
        with self.assertRaisesRegex(RuntimeError, "strict UTF-8 JSON"):
            self.bridge._read_rdc_project_index({})

    def test_resource_index_matches_complete_content_and_never_sets_event(self):
        snapshot = self._begin()

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["matched"])
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertFalse(result["fullFrameTraversal"])
        self.assertFalse(result["physicalApplyAuthority"])
        self.assertEqual(result["resourceOwnershipScope"], "target-capture-session")
        self.assertFalse(result["crossCaptureResourceIdReuse"])
        self.assertEqual(self.controller.events, [])
        asset = result["assets"][0]
        self.assertEqual(asset["ownerResourceId"], 7)
        self.assertEqual(asset["ownerCaptureSessionId"], "1" * 32)
        self.assertTrue(asset["physicalAbiValidated"])
        self.assertTrue(asset["constantBlocksMatched"])
        self.assertEqual(asset["constantBlocks"], self._expected_shader()["constantBlocks"])

        lease = self._commit(snapshot)
        self.assertTrue(lease["committed"])
        self.assertFalse(lease["physicalApplyAuthority"])
        self.assertIn(
            ("2" * 64, "3" * 64), self.bridge._known_catalog_identity_pairs
        )
        self.assertEqual(self.controller.events, [])

    def test_begin_and_commit_are_idempotent(self):
        first = self._begin()
        second = self._begin()
        self.assertEqual(first["jobId"], second["jobId"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

        first_lease = self._commit(first)
        second_lease = self._commit(first)
        self.assertFalse(first_lease["idempotent"])
        self.assertTrue(second_lease["idempotent"])
        self.assertEqual(self.controller.events, [])

    def test_forged_hash_and_extra_or_missing_blocks_fail_closed(self):
        variants = []
        forged = self._expected_shader()
        forged["interfaceHash"] = "4" * 64
        variants.append(forged)

        missing = self._expected_shader()
        missing["constantBlocks"] = []
        variants.append(missing)

        extra = self._expected_shader()
        duplicate = dict(extra["constantBlocks"][0])
        duplicate.update({"index": 1, "path": "Unexpected", "bind": 1})
        extra["constantBlocks"] = [extra["constantBlocks"][0], duplicate]
        variants.append(extra)

        for shader in variants:
            with self.subTest(shader=shader):
                with self.bridge._project_shader_index_jobs_guard:
                    self.bridge._project_shader_index_jobs.clear()
                snapshot = self._begin(shader)
                self.assertEqual(snapshot["status"], "completed")
                self.assertFalse(snapshot["result"]["matched"])
                self.assertEqual(
                    snapshot["result"]["reasonId"],
                    "shader-resource-index-mismatch",
                )
                self.assertEqual(self.controller.events, [])
        self.assertEqual(set(), self.bridge._known_catalog_identity_pairs)

    def test_glsl_is_a_supported_content_addressed_format(self):
        self.controller.api = "OpenGL"
        self.controller.encoding = "GLSL"
        self.controller.reflection.encoding = "GLSL"

        snapshot = self._begin()

        self.assertTrue(snapshot["result"]["matched"])
        self.assertEqual(snapshot["result"]["assets"][0]["encoding"], "glsl")
        self.assertEqual(self.controller.events, [])

    def test_vulkan_null_pipeline_proves_module_only_and_requires_preflight(self):
        self.controller.api = "Vulkan"
        self.controller.encoding = "SPIRV"
        self.controller.reflection.encoding = "SPIRV"

        snapshot = self._begin()

        self.assertTrue(snapshot["result"]["matched"])
        asset = snapshot["result"]["assets"][0]
        self.assertEqual(
            asset["contentMatchMode"],
            "module-sha256-owner-rid-anchor-preflight",
        )
        self.assertFalse(asset["physicalAbiValidated"])
        self.assertEqual(asset["physicalAbiSha256"], "")
        self.assertTrue(asset["requiresAnchorPreflight"])
        self.assertFalse(snapshot["result"]["physicalApplyAuthority"])
        self.assertEqual(self.controller.events, [])

    def test_same_content_under_another_rid_never_inherits_owner_authority(self):
        expected = self._expected_shader()
        expected["resourceId"] = 8
        self.controller.resources = [
            self.controller.resource,
            types.SimpleNamespace(type="shader", resourceId=8),
        ]

        snapshot = self._begin(expected)

        # The fake controller deliberately returns the same module for both RIDs;
        # the result must still retain exactly the requested target-RDC owner.
        self.assertTrue(snapshot["result"]["matched"])
        asset = snapshot["result"]["assets"][0]
        self.assertEqual(asset["resourceId"], 8)
        self.assertEqual(asset["ownerResourceId"], 8)
        self.assertFalse(asset["crossCaptureResourceIdReuse"])

    def test_inventory_discovers_every_unique_shader_once_without_eid_traversal(self):
        shader_8 = types.SimpleNamespace(type="shader", resourceId=8)
        non_shader = types.SimpleNamespace(type="texture", resourceId=99)
        self.controller.resources = [self.controller.resource, non_shader, shader_8]
        reflection_7 = self.controller.reflection
        reflection_8 = types.SimpleNamespace(**vars(self.controller.reflection))
        # Deliberately identical content under another capture-local RID.  The
        # stable key must remain identical while ownership remains distinct.
        reflection_8.rawBytes = reflection_7.rawBytes
        entry_7 = types.SimpleNamespace(name="main", stage="fragment")
        entry_8 = types.SimpleNamespace(name="main", stage="fragment")
        entry_calls = []
        shader_calls = []
        usage_calls = []
        usage_records_seen = []

        def get_entries(resource):
            resource_id = int(resource)
            entry_calls.append(resource_id)
            return [entry_7 if resource_id == 7 else entry_8]

        def get_shader(unused_pipeline, resource, unused_entry):
            del unused_pipeline, unused_entry
            resource_id = int(resource)
            shader_calls.append(resource_id)
            return reflection_7 if resource_id == 7 else reflection_8

        def get_usage(resource):
            resource_id = int(resource)
            usage_calls.append(resource_id)
            values = (20, 10) if resource_id == 7 else (31, 29)

            def records():
                for value in values:
                    usage_records_seen.append((resource_id, value))
                    yield types.SimpleNamespace(eventId=value)

            return records()

        self.controller.GetShaderEntryPoints = get_entries
        self.controller.GetShader = get_shader
        self.controller.GetUsage = get_usage

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request()
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryOnly"])
        self.assertTrue(result["inventoryComplete"])
        self.assertFalse(result["leaseEligible"])
        self.assertFalse(result["matched"])
        self.assertEqual(result["uniqueShaderResourceCount"], 2)
        self.assertEqual(result["processedShaderResourceCount"], 2)
        self.assertEqual(result["indexedShaderResourceCount"], 2)
        self.assertEqual(result["unindexedShaderResourceCount"], 0)
        self.assertEqual(result["unindexedShaderResourceIds"], [])
        self.assertEqual(result["matchedShaderCount"], 2)
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertFalse(result["fullFrameTraversal"])
        self.assertEqual(result["anchorQueryCount"], 2)
        self.assertEqual(result["representativeEidCount"], 2)
        self.assertEqual(result["anchorPerShaderMaximum"], 1)
        self.assertEqual(entry_calls, [7, 8])
        self.assertEqual(shader_calls, [7, 8])
        self.assertEqual(usage_calls, [7, 8])
        self.assertEqual(usage_records_seen, [(7, 20), (8, 31)])
        self.assertEqual(result["anchorUsageRecordCount"], 2)
        self.assertEqual(self.controller.events, [])
        self.assertEqual(snapshot["progress"]["processedResourceCount"], 2)
        self.assertEqual(snapshot["progress"]["totalResourceCount"], 2)
        assets = result["assets"]
        self.assertEqual([asset["resourceId"] for asset in assets], [7, 8])
        self.assertEqual([asset["anchorEventId"] for asset in assets], [20, 31])
        self.assertEqual(
            [asset["anchorSource"] for asset in assets],
            ["resource-usage-index", "resource-usage-index"],
        )
        self.assertEqual(assets[0]["stableKeySha256"], assets[1]["stableKeySha256"])
        self.assertEqual(
            set(assets[0]["stableKeyMaterial"]),
            {
                "api", "encoding", "rawSha256", "stage", "entryPoint",
                "abiHash", "layoutHash",
            },
        )
        self.assertNotIn("resourceId", assets[0]["stableKeyMaterial"])
        self.assertNotIn("anchorEventId", assets[0]["stableKeyMaterial"])
        expected_key = hashlib.sha256(
            self.bridge._canonical_project_index_bytes(
                assets[0]["stableKeyMaterial"]
            )
        ).hexdigest()
        self.assertEqual(assets[0]["stableKeySha256"], expected_key)
        self.assertEqual(len(result["rawBytecodeBlobs"]), 1)
        blob = result["rawBytecodeBlobs"][0]
        self.assertEqual(base64.b64decode(blob["data"]), reflection_7.rawBytes)
        inventory_material = {
            "api": "d3d11",
            "shaders": [
                {
                    "resourceId": asset["resourceId"],
                    "stableKeySha256": asset["stableKeySha256"],
                    "anchorEventId": asset["anchorEventId"],
                    "constantBlocks": asset["constantBlocks"],
                }
                for asset in assets
            ],
        }
        self.assertEqual(
            result["resourceInventorySha256"],
            hashlib.sha256(
                self.bridge._canonical_project_index_bytes(inventory_material)
            ).hexdigest(),
        )
        self.assertTrue(result["symbolSnapshotPublished"])
        self.assertEqual(result["symbolSnapshotShaderCount"], 2)
        self.assertEqual(result["symbolSnapshotUseCount"], 2)
        self.assertEqual(
            result["symbolSnapshotCaptureId"],
            result["stableResourceSetSha256"],
        )
        symbol_snapshot = self.bridge._symbol_capture_snapshot
        self.assertEqual(
            symbol_snapshot["captureId"], result["stableResourceSetSha256"]
        )
        self.assertEqual(len(symbol_snapshot["shaderKeys"]), 2)
        self.assertEqual(
            sorted(value[0] for value in symbol_snapshot["shaderKeys"]), [7, 8]
        )
        self.assertEqual(
            sorted(values[0] for values in symbol_snapshot["usesByKey"].values()),
            [20, 31],
        )
        # Inventory enables knowledge/alias revalidation only.  It must not
        # grant the executable catalog identity fast path.
        self.assertEqual(self.bridge._known_catalog_capture_ids, {})
        self.assertEqual(self.bridge._known_catalog_identity_pairs, set())

    def test_inventory_skips_zero_usage_then_uses_first_positive_eid_once(self):
        class ResourceHandle:
            def __init__(self, value):
                self.value = value

            def __int__(self):
                return self.value

        resource_handle = ResourceHandle(7)
        self.controller.resource.resourceId = resource_handle
        entries = [
            types.SimpleNamespace(name="main", stage="fragment"),
            types.SimpleNamespace(name="alternate", stage="fragment"),
        ]
        usage_calls = []
        usage_records_seen = []
        root_action_calls = []

        def get_entries(resource):
            self.assertIs(resource, resource_handle)
            return list(entries)

        def get_shader(unused_pipeline, resource, entry):
            del unused_pipeline
            self.assertIs(resource, resource_handle)
            reflection = types.SimpleNamespace(**vars(self.controller.reflection))
            reflection.entryPoint = entry.name
            return reflection

        def get_usage(resource):
            self.assertIs(resource, resource_handle)
            usage_calls.append(resource)

            def records():
                for record in (
                    types.SimpleNamespace(eventId=0, usage="Unused"),
                    types.SimpleNamespace(eventId=42, usage="PS_Resource"),
                    types.SimpleNamespace(eventId=99, usage="PS_Resource"),
                ):
                    usage_records_seen.append((record.eventId, record.usage))
                    yield record

            return records()

        def get_root_actions():
            root_action_calls.append(True)
            raise AssertionError(
                "unique-Shader inventory must not traverse the action tree"
            )

        self.controller.GetShaderEntryPoints = get_entries
        self.controller.GetShader = get_shader
        self.controller.GetUsage = get_usage
        self.controller.GetRootActions = get_root_actions

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request()
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["uniqueShaderResourceCount"], 1)
        self.assertEqual(result["inspectedEntryPointCount"], 2)
        self.assertEqual(result["anchorQueryCount"], 1)
        self.assertEqual(result["anchorUsageRecordCount"], 2)
        self.assertEqual(result["representativeEidCount"], 1)
        self.assertEqual(result["anchorPerShaderMaximum"], 1)
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertFalse(result["fullFrameTraversal"])
        self.assertEqual(usage_calls, [resource_handle])
        self.assertEqual(
            usage_records_seen,
            [(0, "Unused"), (42, "PS_Resource")],
        )
        self.assertEqual(root_action_calls, [])
        self.assertEqual(self.controller.events, [])
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(
            [asset["entryPoint"] for asset in result["assets"]],
            ["alternate", "main"],
        )
        self.assertEqual(
            [asset["anchorEventId"] for asset in result["assets"]],
            [42, 42],
        )
        self.assertTrue(all(
            asset["exactExecutableMatchEligible"]
            for asset in result["assets"]
        ))

    def test_inventory_rejects_anchor_beyond_bounded_invalid_usage_prefix(self):
        usage_calls = []
        usage_records_seen = []
        scan_limit = self.bridge.MAX_PROJECT_SHADER_INDEX_ANCHOR_PREFIX_SCAN_RECORDS

        def get_usage(resource):
            usage_calls.append(int(resource))

            def records():
                for index in range(scan_limit):
                    usage_records_seen.append(index)
                    yield types.SimpleNamespace(eventId=0)
                usage_records_seen.append("beyond-limit")
                yield types.SimpleNamespace(eventId=77)

            return records()

        self.controller.GetUsage = get_usage

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request()
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["anchorQueryCount"], 1)
        self.assertEqual(result["anchorUsageRecordCount"], scan_limit)
        self.assertEqual(result["representativeEidCount"], 0)
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertEqual(usage_calls, [7])
        self.assertEqual(usage_records_seen, list(range(scan_limit)))
        asset = result["assets"][0]
        self.assertIsNone(asset["anchorEventId"])
        self.assertIsNone(asset["anchorSource"])
        self.assertFalse(asset["exactExecutableMatchEligible"])
        self.assertIn(
            {"code": "shader-resource-usage-limit", "resourceId": 7},
            result["diagnostics"],
        )
        self.assertEqual(self.controller.events, [])

    def test_inventory_keeps_knowledge_when_no_representative_eid_exists(self):
        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request()
        )

        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["anchorQueryCount"], 0)
        self.assertEqual(result["representativeEidCount"], 0)
        self.assertEqual(result["eventTraversalCount"], 0)
        asset = result["assets"][0]
        self.assertIsNone(asset["anchorEventId"])
        self.assertIsNone(asset["anchorSource"])
        self.assertTrue(asset["stableKeySha256"])
        self.assertFalse(asset["exactExecutableMatchEligible"])
        self.assertTrue(result["symbolSnapshotPublished"])
        key = (
            asset["resourceId"], asset["stage"], asset["entryPoint"],
            asset["rawSha256"],
        )
        self.assertIn(key, self.bridge._symbol_capture_snapshot["shaderKeys"])
        self.assertNotIn(key, self.bridge._symbol_capture_snapshot["usesByKey"])
        self.assertEqual(result["symbolSnapshotUseCount"], 0)
        self.assertEqual(self.controller.events, [])

    def test_inventory_skips_unrepresentable_shader_stage_per_resource(self):
        resource_8 = types.SimpleNamespace(type="shader", resourceId=8)
        self.controller.resources = [self.controller.resource, resource_8]
        entry_calls = []

        def get_entries(resource):
            resource_id = int(resource)
            entry_calls.append(resource_id)
            if resource_id == 7:
                raise ValueError("152 is not a valid ShaderStage")
            return [self.controller.entry]

        self.controller.GetShaderEntryPoints = get_entries

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["uniqueShaderResourceCount"], 2)
        self.assertEqual(result["processedShaderResourceCount"], 2)
        self.assertEqual(result["indexedShaderResourceCount"], 1)
        self.assertEqual(result["unindexedShaderResourceCount"], 1)
        self.assertEqual(result["unindexedShaderResourceIds"], [7])
        self.assertEqual(entry_calls, [7, 8])
        self.assertEqual(
            [asset["resourceId"] for asset in result["assets"]], [8]
        )
        self.assertIn(
            {"code": "shader-entry-points-unavailable", "resourceId": 7},
            result["diagnostics"],
        )
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertFalse(result["fullFrameTraversal"])
        self.assertEqual(result["anchorPerShaderMaximum"], 1)
        self.assertEqual(self.controller.events, [])

    def test_inventory_never_queries_anchor_for_unindexed_shader_resource(self):
        resource_8 = types.SimpleNamespace(type="shader", resourceId=8)
        self.controller.resources = [self.controller.resource, resource_8]
        usage_calls = []

        def get_entries(resource):
            if int(resource) == 7:
                raise ValueError("152 is not a valid ShaderStage")
            return [self.controller.entry]

        def get_usage(resource):
            usage_calls.append(int(resource))
            return [types.SimpleNamespace(eventId=22)]

        self.controller.GetShaderEntryPoints = get_entries
        self.controller.GetUsage = get_usage

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=True)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["indexedShaderResourceCount"], 1)
        self.assertEqual(result["unindexedShaderResourceIds"], [7])
        self.assertEqual(result["anchorQueryCount"], 1)
        self.assertEqual(result["representativeEidCount"], 1)
        self.assertEqual(usage_calls, [8])
        self.assertEqual(result["assets"][0]["resourceId"], 8)
        self.assertEqual(result["assets"][0]["anchorEventId"], 22)
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertEqual(self.controller.events, [])

    def test_inventory_does_not_eagerly_read_invalid_entry_stage_fallback(self):
        class EntryWithInvalidStage:
            name = "main"

            @property
            def stage(self):
                raise ValueError("72 is not a valid ShaderStage")

        self.controller.GetShaderEntryPoints = (
            lambda unused_resource: [EntryWithInvalidStage()]
        )

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["processedShaderResourceCount"], 1)
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(result["assets"][0]["stage"], "fragment")
        self.assertNotIn(
            "shader-stage-unavailable",
            {item["code"] for item in result["diagnostics"]},
        )
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertEqual(self.controller.events, [])

    def test_inventory_fails_closed_when_reflection_and_entry_stages_are_unknown(self):
        request = self._inventory_request(discover_anchors=False)

        class InvalidStage:
            @property
            def stage(self):
                raise ValueError("232 is not a valid ShaderStage")

        class ReflectionWithInvalidStage(InvalidStage):
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                return getattr(self.source, name)

        class EntryWithInvalidStage(InvalidStage):
            name = "main"

        self.controller.reflection = ReflectionWithInvalidStage(
            self.controller.reflection
        )
        self.controller.GetShaderEntryPoints = (
            lambda unused_resource: [EntryWithInvalidStage()]
        )

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            request
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(result["processedShaderResourceCount"], 1)
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["indexedShaderResourceCount"], 0)
        self.assertEqual(result["unindexedShaderResourceIds"], [7])
        self.assertIn(
            "shader-stage-unavailable",
            {item["code"] for item in result["diagnostics"]},
        )
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertEqual(self.controller.events, [])

    def test_inventory_excludes_ambiguous_cbuffer_paths_without_losing_shader(self):
        scalar = types.SimpleNamespace(
            name="float",
            baseType="Float",
            rows=1,
            columns=1,
            elements=1,
            arrayByteStride=0,
            matrixByteStride=0,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[],
        )
        first = types.SimpleNamespace(
            name="unknown", byteOffset=0, type=scalar,
            bitFieldOffset=0, bitFieldSize=0,
        )
        second = types.SimpleNamespace(
            name="unknown", byteOffset=4, type=scalar,
            bitFieldOffset=0, bitFieldSize=0,
        )
        self.controller.reflection.constantBlocks = [
            _constant_block(byte_size=16)
        ]
        self.controller.reflection.constantBlocks[0].variables = [first, second]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(result["assets"][0]["constantBlocks"][0]["fields"], [])
        self.assertIn(
            {
                "code": "shader-cbuffer-field-path-ambiguous",
                "resourceId": 7,
                "stage": "fragment",
                "entryPoint": "main",
                "count": 1,
            },
            result["diagnostics"],
        )
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertEqual(self.controller.events, [])

    def test_inventory_projects_rich_cbuffer_leaves_without_resolving_overlap(self):
        float4 = types.SimpleNamespace(
            name="float4",
            baseType="VarType.Float",
            rows=1,
            columns=4,
            elements=1,
            arrayByteStride=0,
            matrixByteStride=0,
            flags=3,
            pointerTypeID=17,
            members=[],
        )
        scalar = types.SimpleNamespace(
            name="float",
            baseType="Float",
            rows=1,
            columns=1,
            elements=1,
            arrayByteStride=0,
            matrixByteStride=0,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[],
        )
        nested_leaf = types.SimpleNamespace(
            name="roughness",
            byteOffset=8,
            bitFieldOffset=2,
            bitFieldSize=5,
            type=scalar,
        )
        nested_type = types.SimpleNamespace(
            name="MaterialParams",
            baseType="Struct",
            rows=1,
            columns=1,
            elements=1,
            arrayByteStride=0,
            matrixByteStride=0,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[nested_leaf],
        )
        color = types.SimpleNamespace(
            name="color",
            byteOffset=0,
            bitFieldOffset=0,
            bitFieldSize=0,
            type=float4,
        )
        # A second reflected leaf at the same physical offset is intentionally
        # retained; the project layer must fail closed if an alias target is
        # ambiguous instead of the bridge choosing the first name.
        overlap = types.SimpleNamespace(
            name="tintAlias",
            byteOffset=0,
            bitFieldOffset=0,
            bitFieldSize=0,
            type=float4,
        )
        nested = types.SimpleNamespace(
            name="params",
            byteOffset=16,
            bitFieldOffset=0,
            bitFieldSize=0,
            type=nested_type,
        )
        block = _constant_block(byte_size=64)
        block.variables = [nested, overlap, color]
        self.controller.reflection.constantBlocks = [block]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        projected = result["assets"][0]["constantBlocks"][0]
        self.assertEqual(projected["name"], "PerMaterial")
        expected_keys = {
            "canonicalPath", "name", "absoluteByteOffset", "byteSize",
            "baseType", "rows", "columns", "elements",
            "arrayByteStride", "matrixByteStride", "rowMajor", "flags",
            "pointerTypeID", "bitFieldOffset", "bitFieldSize",
        }
        self.assertEqual(len(projected["fields"]), 3)
        self.assertTrue(all(set(field) == expected_keys
                            for field in projected["fields"]))
        self.assertEqual(
            [(field["absoluteByteOffset"], field["canonicalPath"])
             for field in projected["fields"]],
            [(0, "color"), (0, "tintAlias"), (24, "params.roughness")],
        )
        self.assertEqual(projected["fields"][0]["baseType"], "Float")
        self.assertEqual(projected["fields"][0]["byteSize"], 16)
        self.assertEqual(projected["fields"][0]["flags"], 3)
        self.assertEqual(projected["fields"][0]["pointerTypeID"], 17)
        nested_projected = projected["fields"][2]
        self.assertEqual(nested_projected["bitFieldOffset"], 2)
        self.assertEqual(nested_projected["bitFieldSize"], 5)

        inventory_material = {
            "api": "d3d11",
            "shaders": [{
                "resourceId": result["assets"][0]["resourceId"],
                "stableKeySha256": result["assets"][0]["stableKeySha256"],
                "anchorEventId": None,
                "constantBlocks": [projected],
            }],
        }
        self.assertEqual(
            result["resourceInventorySha256"],
            hashlib.sha256(self.bridge._canonical_project_index_bytes(
                inventory_material)).hexdigest(),
        )
        self.assertEqual(self.controller.events, [])

    def test_inventory_cross_capture_publication_resets_alias_overlay(self):
        self.bridge._symbol_capture_snapshot = {
            "captureId": "a" * 64,
            "capturePath": str(Path(self.temporary.name) / "old.rdc"),
            "shaderKeys": {(99, "fragment", "main", "b" * 64)},
            "usesByKey": {(99, "fragment", "main", "b" * 64): [3]},
        }
        self.bridge._symbol_alias_overlay = {
            "schema": self.bridge.SYMBOL_ALIAS_SCHEMA,
            "schemaVersion": self.bridge.SYMBOL_ALIAS_VERSION,
            "captureId": "a" * 64,
            "sourcePath": "old.json",
            "confirmedOnly": True,
            "allAliases": [{"displayName": "old"}],
            "aliases": [{"displayName": "old"}],
        }

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(snapshot["result"]["symbolSnapshotPublished"])
        self.assertEqual(self.bridge._symbol_alias_overlay["aliases"], [])
        self.assertEqual(
            self.bridge._symbol_capture_snapshot["capturePath"],
            self.bridge.os.path.normcase(str(self.capture_path.resolve())),
        )

    def test_inventory_replay_callback_defers_symbol_commit_until_gui_poll(self):
        callbacks = []
        self.bridge._ctx.replay.AsyncInvoke = (
            lambda callback, tag: callbacks.append((tag, callback)))

        queued = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(len(callbacks), 1)
        tag, callback = callbacks.pop(0)
        self.assertTrue(tag.startswith(
            self.bridge.PROJECT_SHADER_INDEX_REPLAY_TAG_PREFIX))
        callback(self.controller)
        self.assertEqual(
            self.bridge._project_shader_index_jobs[queued["jobId"]]["status"],
            "running",
        )
        self.assertEqual(len(callbacks), 1)
        while callbacks:
            next_tag, callback = callbacks.pop(0)
            self.assertEqual(next_tag, tag)
            callback(self.controller)
        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        self.assertEqual(job["status"], "completed")
        self.assertFalse(job["result"]["symbolSnapshotPublished"])
        self.assertEqual(self.bridge._symbol_capture_snapshot["captureId"], "")

        completed = self.bridge._poll_rdc_project_shader_resource_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "job_id": queued["jobId"],
        })

        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["symbolSnapshotPublished"])
        self.assertEqual(
            self.bridge._symbol_capture_snapshot["captureId"],
            completed["result"]["stableResourceSetSha256"],
        )
        self.assertEqual(self.controller.events, [])

    def test_inventory_replay_slices_one_physical_shader_and_yields_to_set_event(self):
        callbacks = []
        foreground_runs = []
        inspected_resources = []
        self.controller.resources = [
            types.SimpleNamespace(type="shader", resourceId=resource_id)
            for resource_id in (7, 8, 9)
        ]

        def get_entries(resource):
            inspected_resources.append(int(resource))
            if len(inspected_resources) == 1:
                callbacks.append((
                    "replaySetEvent",
                    lambda unused_controller: foreground_runs.append(True),
                ))
            return [self.controller.entry]

        self.controller.GetShaderEntryPoints = get_entries
        self.bridge._ctx.replay.AsyncInvoke = (
            lambda callback, tag: callbacks.append((tag, callback)))

        queued = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )
        background_tag, first_slice = callbacks.pop(0)
        first_slice(self.controller)

        self.assertEqual(inspected_resources, [7])
        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        suspended = job["steps"].gi_frame.f_locals
        for local_name in (
            "ctrl",
            "resources",
            "description",
            "resource",
            "entries",
            "entry",
            "reflection",
            "reflected_blocks",
            "reflected_block",
        ):
            self.assertIsNone(
                suspended.get(local_name),
                "suspended inventory retained %s" % local_name,
            )
        self.assertTrue(all(
            isinstance(value, int)
            for value in suspended["by_id"].values()
        ))
        self.assertEqual(
            [tag for tag, unused_callback in callbacks],
            ["replaySetEvent", background_tag],
        )
        unused_tag, set_event = callbacks.pop(0)
        set_event(self.controller)
        self.assertEqual(foreground_runs, [True])
        self.assertEqual(inspected_resources, [7])

        while callbacks:
            tag, callback = callbacks.pop(0)
            inspected_before = len(inspected_resources)
            callback(self.controller)
            if tag == background_tag:
                self.assertLessEqual(
                    len(inspected_resources) - inspected_before, 1
                )

        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(inspected_resources, [7, 8, 9])

    def test_inventory_replay_yields_gil_before_requeue_but_not_on_completion(self):
        callbacks = []
        calls = []

        def queue_slice(callback, tag):
            calls.append(("queue", tag))
            callbacks.append((tag, callback))

        original_sleep = self.bridge.time.sleep
        self.bridge.time.sleep = (
            lambda seconds: calls.append(("yield", seconds)))
        self.bridge._ctx.replay.AsyncInvoke = queue_slice
        try:
            queued = self.bridge._begin_rdc_project_shader_resource_index(
                self._inventory_request(discover_anchors=False)
            )
            background_tag, first_slice = callbacks.pop(0)
            calls.append(("run", 1))
            first_slice(self.controller)
            calls.append(("return", 1))

            self.assertEqual(
                [name for name, unused_value in calls],
                ["queue", "run", "yield", "queue", "return"],
            )
            self.assertEqual(calls[0], ("queue", background_tag))
            self.assertEqual(
                calls[2],
                ("yield", self.bridge.PROJECT_SHADER_INDEX_UI_YIELD_SECONDS),
            )
            self.assertEqual(calls[3], ("queue", background_tag))
            self.assertEqual(len(callbacks), 1)
            next_tag, final_slice = callbacks.pop(0)
            self.assertEqual(next_tag, background_tag)

            calls.append(("run", 2))
            final_slice(self.controller)
            calls.append(("return", 2))
        finally:
            self.bridge.time.sleep = original_sleep

        self.assertEqual(
            [name for name, unused_value in calls],
            [
                "queue", "run", "yield", "queue", "return",
                "run", "return",
            ],
        )
        self.assertEqual(callbacks, [])
        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        self.assertEqual(job["status"], "completed", job.get("error"))

    def test_inventory_replay_uses_only_current_callback_controller_and_proxies(self):
        class InvocationToken:
            def __init__(self):
                self.alive = True

            def check(self):
                if not self.alive:
                    raise RuntimeError("expired invocation proxy")

        class ScopedRecord:
            def __init__(self, token, **values):
                self._token = token
                self._values = values

            def __getattr__(self, name):
                self._token.check()
                try:
                    return self._values[name]
                except KeyError:
                    raise AttributeError(name)

        class ScopedResourceId:
            def __init__(self, token, value):
                self._token = token
                self._value = value

            def __int__(self):
                self._token.check()
                return self._value

        issued = {
            "controllers": [],
            "resources": [],
            "entries": [],
            "reflections": [],
            "usages": [],
        }

        class ScopedController:
            def __init__(self, token):
                self._token = token
                issued["controllers"].append(self)

            def _check(self):
                self._token.check()

            def GetAPIProperties(self):
                self._check()
                return ScopedRecord(self._token, pipelineType="D3D11")

            def GetResources(self):
                self._check()
                descriptions = []
                for value in (7, 8):
                    resource = ScopedResourceId(self._token, value)
                    issued["resources"].append(resource)
                    descriptions.append(ScopedRecord(
                        self._token, type="shader", resourceId=resource))
                return descriptions

            def GetShaderEntryPoints(self, resource):
                self._check()
                int(resource)
                entry = ScopedRecord(
                    self._token, name="main", stage="fragment")
                issued["entries"].append(entry)
                return [entry]

            def GetShader(self, unused_pipeline, resource, entry):
                self._check()
                resource_id = int(resource)
                self.assert_current_entry(entry)
                reflection = ScopedRecord(
                    self._token,
                    rawBytes=("captured-shader-%d" % resource_id).encode("ascii"),
                    stage="fragment",
                    entryPoint="main",
                    encoding="dxbc",
                    inputSignature=[],
                    outputSignature=[],
                    readOnlyResources=[],
                    readWriteResources=[],
                    samplers=[],
                    constantBlocks=[],
                    interfaces=[],
                    outputTopology="",
                    dispatchThreadsDimension=[0, 0, 0],
                )
                issued["reflections"].append(reflection)
                return reflection

            def assert_current_entry(self, entry):
                self._check()
                if entry.name != "main":
                    raise AssertionError("unexpected shader entry")

            def GetUsage(self, resource):
                self._check()
                resource_id = int(resource)
                usage = ScopedRecord(
                    self._token, eventId=100 + resource_id)
                issued["usages"].append(usage)
                return [usage]

        callbacks = []
        self.bridge._ctx.replay.AsyncInvoke = (
            lambda callback, tag: callbacks.append((tag, callback)))
        queued = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=True)
        )

        retired = None
        callback_tokens = []
        while callbacks:
            unused_tag, callback = callbacks.pop(0)
            token = InvocationToken()
            callback_tokens.append(token)
            controller = ScopedController(token)
            before = {name: len(values) for name, values in issued.items()}
            try:
                callback(controller)
            finally:
                token.alive = False
            if retired is None and all(
                    len(issued[name]) > before[name]
                    for name in (
                        "resources", "entries", "reflections", "usages")):
                retired = (
                    controller,
                    issued["resources"][before["resources"]],
                    issued["entries"][before["entries"]],
                    issued["reflections"][before["reflections"]],
                    issued["usages"][before["usages"]],
                )

        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertTrue(job["result"]["inventoryComplete"])
        self.assertEqual(
            [asset["resourceId"] for asset in job["result"]["assets"]],
            [7, 8],
        )
        self.assertGreaterEqual(len(callback_tokens), 3)
        self.assertEqual(
            len({id(controller) for controller in issued["controllers"]}),
            len(callback_tokens),
        )

        self.assertIsNotNone(retired)
        old_controller, old_resource, old_entry, old_reflection, old_usage = retired
        expired_accesses = (
            old_controller.GetResources,
            lambda: int(old_resource),
            lambda: old_entry.name,
            lambda: old_reflection.rawBytes,
            lambda: old_usage.eventId,
        )
        for access in expired_accesses:
            with self.assertRaisesRegex(RuntimeError, "expired invocation proxy"):
                access()

    def test_inventory_replay_slice_stops_on_capture_change_boundary(self):
        callbacks = []
        inspected_resources = []
        self.controller.resources = [
            types.SimpleNamespace(type="shader", resourceId=resource_id)
            for resource_id in (7, 8)
        ]

        def get_entries(resource):
            inspected_resources.append(int(resource))
            return [self.controller.entry]

        self.controller.GetShaderEntryPoints = get_entries
        self.bridge._ctx.replay.AsyncInvoke = (
            lambda callback, tag: callbacks.append((tag, callback)))

        queued = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )
        unused_tag, first_slice = callbacks.pop(0)
        first_slice(self.controller)
        self.assertEqual(inspected_resources, [7])

        self.bridge._capture_session_id = "6" * 32
        unused_tag, second_slice = callbacks.pop(0)
        second_slice(self.controller)

        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        self.assertEqual(job["status"], "failed")
        self.assertIn("capture changed", job["error"])
        self.assertEqual(inspected_resources, [7])
        self.assertIsNone(job["steps"])

    def test_inventory_replay_slice_stops_on_cancel_boundary(self):
        callbacks = []
        inspected_resources = []
        self.controller.resources = [
            types.SimpleNamespace(type="shader", resourceId=resource_id)
            for resource_id in (7, 8)
        ]

        def get_entries(resource):
            inspected_resources.append(int(resource))
            return [self.controller.entry]

        self.controller.GetShaderEntryPoints = get_entries
        self.bridge._ctx.replay.AsyncInvoke = (
            lambda callback, tag: callbacks.append((tag, callback)))

        queued = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )
        unused_tag, first_slice = callbacks.pop(0)
        first_slice(self.controller)
        self.assertEqual(inspected_resources, [7])

        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._request_project_shader_index_cancel(
                job, self.bridge.time.monotonic(), "test cancellation"
            )
        unused_tag, second_slice = callbacks.pop(0)
        second_slice(self.controller)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "test cancellation")
        self.assertEqual(inspected_resources, [7])
        self.assertIsNone(job["steps"])

    def test_inventory_publication_fails_closed_with_alias_recovery_ledger(self):
        old_snapshot = {
            "captureId": "a" * 64,
            "capturePath": str(Path(self.temporary.name) / "old.rdc"),
            "shaderKeys": {(99, "fragment", "main", "b" * 64)},
            "usesByKey": {(99, "fragment", "main", "b" * 64): [3]},
        }
        self.bridge._symbol_capture_snapshot = old_snapshot
        self.bridge._symbol_alias_recovery_ledger = {"state": "committed"}

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("Rollback", snapshot["error"])
        self.assertIs(self.bridge._symbol_capture_snapshot, old_snapshot)
        self.assertEqual(self.controller.events, [])

    def test_cbuffer_free_shader_has_a_complete_empty_layout_hash(self):
        self.controller.reflection.constantBlocks = []

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        asset = snapshot["result"]["assets"][0]
        self.assertEqual(
            asset["layoutHash"], self.bridge._canonical_json_sha256([])
        )
        self.assertTrue(asset["stableKeyComplete"])
        self.assertTrue(asset["stableKeySha256"])

    def test_inventory_cbuffer_name_falls_back_to_stable_block_path(self):
        self.controller.reflection.constantBlocks = [_constant_block(name="   ")]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        block = snapshot["result"]["assets"][0]["constantBlocks"][0]
        self.assertEqual(block["path"], "cbuffer0")
        self.assertEqual(block["name"], "cbuffer0")

    def test_inventory_invalid_cbuffer_size_fails_closed(self):
        self.controller.reflection.constantBlocks = [
            _constant_block(byte_size=0)
        ]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        result = snapshot["result"]
        self.assertFalse(result["inventoryComplete"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-reflection-block-invalid",
        )
        self.assertEqual(self.bridge._symbol_capture_snapshot["captureId"], "")
        self.assertEqual(self.controller.events, [])

    def test_inventory_skips_native_dxil_empty_struct_declarations(self):
        empty_type = types.SimpleNamespace(
            name="empty_struct", baseType="VarType.Struct", members=[],
            rows=0, columns=0, elements=1, arrayByteStride=0,
            matrixByteStride=0, pointerTypeID=0xFFFFFFFF)
        scalar_type = types.SimpleNamespace(
            name="float", baseType="VarType.Float", members=[],
            rows=1, columns=1, elements=1, arrayByteStride=0,
            matrixByteStride=0, pointerTypeID=0xFFFFFFFF)

        def field(name, offset, shader_type):
            return types.SimpleNamespace(
                name=name, byteOffset=offset, type=shader_type,
                bitFieldOffset=0, bitFieldSize=0)

        nested_type = types.SimpleNamespace(
            name="nested_empty", baseType="VarType.Struct", members=[
                field("value", 0, scalar_type), field("b", 4, empty_type)])
        block = _constant_block(byte_size=32)
        block.variables = [field("empty", 0, empty_type),
                           field("value", 0, scalar_type),
                           field("nested_empty", 16, nested_type)]
        self.controller.encoding = self.controller.reflection.encoding = "DXIL"
        self.controller.reflection.constantBlocks = [block]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False))

        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"], result)
        asset = result["assets"][0]
        fields = asset["constantBlocks"][0]["fields"]
        self.assertEqual(
            [(item["canonicalPath"], item["absoluteByteOffset"], item["byteSize"])
             for item in fields], [("value", 0, 4), ("nested_empty.value", 16, 4)])

    def test_inventory_zero_byte_leaf_fails_closed_with_diagnostic(self):
        invalid_leaf = types.SimpleNamespace(
            name="opaque",
            byteOffset=0,
            bitFieldOffset=0,
            bitFieldSize=0,
            type=None,
        )
        block = _constant_block()
        block.variables = [invalid_leaf]
        self.controller.reflection.constantBlocks = [block]

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertFalse(result["inventoryComplete"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-reflection-field-invalid",
        )
        self.assertIn(
            "shader-resource-index-reflection-field-invalid",
            [item["code"] for item in result["diagnostics"]],
        )
        self.assertFalse(result.get("symbolSnapshotPublished", False))
        self.assertEqual(self.bridge._symbol_capture_snapshot["captureId"], "")
        self.assertEqual(self.controller.events, [])

    def test_inventory_rejects_multiple_representative_eids_for_one_resource(self):
        first = self._expected_shader()
        second = dict(first)
        second["entryPoint"] = "alternate"
        second["anchorEventId"] = first["anchorEventId"] + 1

        with self.assertRaisesRegex(ValueError, "more than one representative EID"):
            self.bridge._begin_rdc_project_shader_resource_index(
                self._inventory_request(expected_shaders=[first, second])
            )

    def test_vulkan_inventory_is_stable_knowledge_but_not_executable_authority(self):
        self.controller.api = "Vulkan"
        self.controller.encoding = "SPIRV"
        self.controller.reflection.encoding = "SPIRV"

        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._inventory_request(discover_anchors=False)
        )

        result = snapshot["result"]
        self.assertTrue(result["inventoryComplete"])
        asset = result["assets"][0]
        self.assertTrue(asset["stableKeySha256"])
        self.assertTrue(asset["abiHash"])
        self.assertTrue(asset["layoutHash"])
        self.assertFalse(asset["stableKeyComplete"])
        self.assertFalse(asset["physicalAbiValidated"])
        self.assertTrue(asset["requiresAnchorPreflight"])
        self.assertFalse(asset["exactExecutableMatchEligible"])
        self.assertFalse(result["physicalApplyAuthority"])
        with self.assertRaisesRegex(RuntimeError, "incomplete or changed"):
            self._commit(snapshot)

    def test_job_timeout_ttl_and_capture_change_fail_closed(self):
        replay = self.bridge._ctx.replay
        replay.AsyncInvoke = lambda unused_callback, unused_tag: None
        queued = self._begin()
        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        job["createdMonotonic"] -= (
            self.bridge.PROJECT_SHADER_INDEX_JOB_TIMEOUT_SECONDS + 1.0
        )
        timed_out = self.bridge._poll_rdc_project_shader_resource_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "job_id": queued["jobId"],
        })
        self.assertEqual(timed_out["status"], "failed")
        self.assertIn("timed out", timed_out["error"])

        job["updatedMonotonic"] -= (
            self.bridge.PROJECT_SHADER_INDEX_JOB_TTL_SECONDS + 1.0
        )
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.bridge._poll_rdc_project_shader_resource_index({
                "expected_capture_path": str(self.capture_path),
                "expected_capture_session_id": "1" * 32,
                "job_id": queued["jobId"],
            })

        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        replay.AsyncInvoke = (
            lambda callback, unused_tag: callback(self.controller))
        completed = self._begin()
        self.bridge._capture_session_id = "6" * 32
        with self.assertRaisesRegex(RuntimeError, "capture session changed"):
            self._commit(completed)

    def test_vulkan_runtime_storage_arrays_preserve_type_identity_in_target_inventory(self):
        self.controller.api = "Vulkan"
        self.controller.reflection.encoding = "SPIR-V"
        runtime_type = types.SimpleNamespace(
            name="float[]", baseType="float", members=[], elements=0xFFFFFFFF,
            arrayByteStride=4, rows=1, columns=1)
        member = types.SimpleNamespace(name="values", byteOffset=0, type=runtime_type)
        storage_type = types.SimpleNamespace(name="Storage", baseType="struct", members=[member], elements=1)
        storage = types.SimpleNamespace(name="StorageBuffer", variableType=storage_type,
            fixedBindNumber=16, fixedBindSetOrSpace=0, bindArraySize=1,
            descriptorType="ReadWriteBuffer", textureType="Buffer", isReadOnly=False)
        self.controller.reflection.readWriteResources = [storage]
        snapshot = self.bridge._begin_rdc_project_shader_resource_index(
            self._target_request([hashlib.sha256(self.controller.raw).hexdigest()]))
        result = snapshot["result"]
        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(result["targetInventoryComplete"])
        self.assertEqual(result["reasonId"], "")
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(runtime_type.elements, 0xFFFFFFFF)
        self.assertEqual(self.controller.events, [])
        self.assertFalse(result["gpuReplayMutation"])

        for invalid in (0xFFFFFFFE, -1, 1 << 21):
            with self.subTest(elements=invalid):
                runtime_type.elements = invalid
                with self.assertRaises(self.bridge._ProjectShaderIndexLimit):
                    self.bridge._validate_project_shader_reflection_budget(
                        self.controller.reflection, self.bridge._new_project_shader_reflection_budget())
        runtime_type.elements = 0xFFFFFFFF
        self.controller.reflection.readWriteResources = []
        self.controller.reflection.constantBlocks[0].variables = [member]
        with self.assertRaises(self.bridge._ProjectShaderIndexLimit):
            self.bridge._validate_project_shader_reflection_budget(
                self.controller.reflection, self.bridge._new_project_shader_reflection_budget())

    def test_request_and_reflection_budgets_fail_before_event_traversal(self):
        expected = self._expected_shader()
        request = self._index_request(expected)
        request["expected_shaders"] = [
            dict(request["expected_shaders"][0], resourceId=index + 1)
            for index in range(self.bridge.MAX_PROJECT_SHADER_INDEX_EXPECTED + 1)
        ]
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            self.bridge._begin_rdc_project_shader_resource_index(request)

        leaf_type = types.SimpleNamespace(
            name="float", baseType="float", members=[], elements=1
        )
        child = types.SimpleNamespace(name="leaf", byteOffset=0, type=leaf_type)
        for depth in range(
            self.bridge.MAX_PROJECT_SHADER_INDEX_REFLECTION_DEPTH + 2
        ):
            nested_type = types.SimpleNamespace(
                name="Nested%d" % depth,
                baseType="struct",
                members=[child],
                elements=1,
            )
            child = types.SimpleNamespace(
                name="field%d" % depth, byteOffset=0, type=nested_type
            )
        self.controller.reflection.constantBlocks = [
            types.SimpleNamespace(
                name="Deep",
                fixedBindNumber=0,
                fixedBindSetOrSpace=0,
                bindArraySize=1,
                byteSize=16,
                bufferBacked=True,
                inlineDataBytes=False,
                compileConstants=False,
                variables=[child],
            )
        ]
        deep_expected = dict(expected)
        deep_expected["constantBlocks"] = []
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        snapshot = self._begin(deep_expected)
        self.assertFalse(snapshot["result"]["matched"])
        self.assertEqual(
            snapshot["result"]["reasonId"],
            "shader-resource-index-reflection-depth-limit",
        )
        self.assertEqual(self.controller.events, [])

        self.controller.reflection.constantBlocks = [_constant_block()]
        signature = types.SimpleNamespace(
            varName="value",
            semanticName="TEXCOORD",
            semanticIdxName="TEXCOORD0",
            semanticIndex=0,
            regIndex=0,
            systemValue="",
            varType="float",
            compType="float",
            compCount=4,
            stream=0,
            regChannelMask=15,
            channelUsedMask=15,
            perPrimitiveRate=False,
            needSemanticIndex=False,
        )
        self.controller.reflection.inputSignature = [
            signature
        ] * (self.bridge.MAX_PROJECT_SHADER_INDEX_SIGNATURES + 1)
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        snapshot = self._begin(expected)
        self.assertFalse(snapshot["result"]["matched"])
        self.assertEqual(
            snapshot["result"]["reasonId"],
            "shader-resource-index-reflection-signature-limit",
        )
        self.assertEqual(self.controller.events, [])


if __name__ == "__main__":
    unittest.main()
