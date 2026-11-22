from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest

from tests.test_dxil_access_signature import REAL_SHAPE_DXIL
from src.shader_workspace.adapters.dxil_access_signature import (
    extract_dxil_cbuffer_access_signature as extract_host_signature,
)


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
PARSER_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "dxil_access_signature.py"


def load_bridge_parser():
    spec = importlib.util.spec_from_file_location(
        "replaylab_bridge_dxil_parser_contract", PARSER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResourceId:
    def __init__(self, value=0):
        self.value = value

    def __int__(self):
        return self.value


def load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ResourceId = ResourceId
    fake_renderdoc.ShaderEncoding = types.SimpleNamespace(DXIL="DXIL")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Pixel="Pixel", Fragment="Pixel"
    )
    fake_renderdoc.ShaderCompileFlags = lambda: object()
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "replaylab_bridge_dxil_apply_contract", BRIDGE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


def ambiguous_block():
    unknown_type = types.SimpleNamespace(
        name="",
        baseType="VarType.UInt",
        rows=1,
        columns=4,
        elements=0,
        arrayByteStride=16,
        matrixByteStride=16,
        flags=1,
        pointerTypeID=0xFFFFFFFF,
        members=[],
        RowMajor=lambda: True,
    )
    return types.SimpleNamespace(
        name="cbuffer0",
        fixedBindNumber=0,
        fixedBindSetOrSpace=0,
        bindArraySize=1,
        byteSize=8,
        bufferBacked=True,
        inlineDataBytes=False,
        compileConstants=False,
        variables=[
            types.SimpleNamespace(
                name="unknown", byteOffset=0, type=unknown_type
            )
        ],
    )


def reflection(blob):
    blocks = [ambiguous_block()]
    for bind, byte_size in ((1, 7572), (2, 528), (3, 860)):
        blocks.append(types.SimpleNamespace(
            name="cbuffer%d" % bind,
            fixedBindNumber=bind,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=byte_size,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=[],
        ))
    return types.SimpleNamespace(
        stage="Pixel",
        entryPoint="main",
        encoding="DXIL",
        rawBytes=blob,
        inputSignature=[],
        outputSignature=[],
        readOnlyResources=[],
        readWriteResources=[],
        samplers=[],
        constantBlocks=blocks,
        interfaces=[],
        outputTopology="TriangleList",
        dispatchThreadsDimension=[0, 0, 0],
    )


class BridgeDxilAccessGateTests(unittest.TestCase):
    def test_candidate_reflection_does_not_repeat_dxil_text_disassembly(self):
        module = load_bridge()
        candidate_blob = b"candidate native DXIL"
        candidate_reflection = reflection(candidate_blob)

        class Controller:
            def __init__(self):
                self.freed = []

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.D3D12")

            def GetTargetShaderEncodings(self):
                return ["DXIL"]

            def BuildTargetShader(self, entry, encoding, source, flags, stage):
                return ResourceId(91), ""

            def GetShaderEntryPoints(self, shader):
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                return candidate_reflection

            def DisassembleShader(self, pipeline, shader_reflection, target):
                raise AssertionError("reflection must not print DXIL text")

            def FreeTargetResource(self, shader):
                self.freed.append(int(shader))

        controller = Controller()
        module._ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: True)
        module._replay = lambda callback: callback(controller)

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate.dxil"
            candidate.write_bytes(candidate_blob)
            result = module._reflect_shader_candidate({
                "candidate_path": str(candidate),
                "expected_sha256": hashlib.sha256(candidate_blob).hexdigest(),
                "stage": "fragment",
                "entry_point": "main",
                "encoding": "dxil",
            })

        self.assertTrue(result["reflectionSucceeded"])
        self.assertTrue(result["freeTargetResourceSucceeded"])
        self.assertEqual(controller.freed, [91])
        first_block = result["compiledInterface"]["constantBlocks"][0]
        self.assertEqual(first_block["layoutCompleteness"], "ambiguous")
        self.assertEqual(
            first_block["layoutEvidence"]["typeAnnotation"], "unavailable"
        )

    def test_bridge_signature_gate_ignores_usage_analysis_but_not_declaration(self):
        module = load_bridge()

        def parameter(used_mask, component_count=4, register_mask=15):
            return types.SimpleNamespace(
                varName="debugName",
                semanticName="TEXCOORD",
                semanticIdxName="TEXCOORD1",
                semanticIndex=1,
                regIndex=4,
                systemValue="ShaderBuiltin.Undefined",
                varType="VarType.Float",
                compCount=component_count,
                stream=0,
                regChannelMask=register_mask,
                channelUsedMask=used_mask,
                perPrimitiveRate=False,
                needSemanticIndex=True,
            )

        baseline = reflection(b"baseline")
        candidate = reflection(b"candidate")
        baseline.constantBlocks = []
        candidate.constantBlocks = []
        baseline.inputSignature = [parameter(15)]
        candidate.inputSignature = [parameter(0)]
        self.assertTrue(
            module._physical_shader_interfaces_compatible(
                baseline, candidate
            )
        )

        candidate.inputSignature = [parameter(0, component_count=3)]
        self.assertFalse(
            module._physical_shader_interfaces_compatible(
                baseline, candidate
            )
        )
        candidate.inputSignature = [parameter(0, register_mask=7)]
        self.assertFalse(
            module._physical_shader_interfaces_compatible(
                baseline, candidate
            )
        )

    def test_unique_dxil_entry_accepts_only_known_main_pipeline_placeholder(self):
        module = load_bridge()
        blob = b"captured native DXIL"
        original_id = ResourceId(77)

        class Pipeline:
            def __init__(self, pipeline_entry):
                self.pipeline_entry = pipeline_entry

            def GetShader(self, stage):
                return original_id

            def GetShaderEntryPoint(self, stage):
                return self.pipeline_entry

            def GetGraphicsPipelineObject(self):
                return ResourceId(5)

        class Controller:
            def __init__(self, pipeline_entry, entries):
                self.pipeline = Pipeline(pipeline_entry)
                self.entries = entries

            def SetFrameEvent(self, event, force):
                return None

            def GetPipelineState(self):
                return self.pipeline

            def GetShaderEntryPoints(self, shader):
                return [
                    types.SimpleNamespace(name=name, stage="Pixel")
                    for name in self.entries
                ]

            def GetShader(self, pipeline, shader, entry):
                result = reflection(blob)
                result.entryPoint = entry.name
                result.encoding = "DXBC"
                return result

        event = {
            "eventId": 321,
            "expectedOriginalResourceId": 77,
            "expectedOriginalSha256": hashlib.sha256(blob).hexdigest(),
        }

        accepted = module._validate_live_shader_event(
            Controller("main", ["MainPS"]), event, "Pixel", "fragment",
            "MainPS", require_d3d11_state=False
        )
        self.assertEqual(77, int(accepted["originalResource"]))

        rejected = (
            ("DifferentPS", ["MainPS"]),
            ("main", ["MainPS", "OtherPS"]),
            ("OtherPS", ["MainPS", "OtherPS"]),
        )
        for pipeline_entry, entries in rejected:
            with self.subTest(
                    pipeline_entry=pipeline_entry, entries=entries):
                with self.assertRaisesRegex(
                        RuntimeError, "pipeline entry point changed"):
                    module._validate_live_shader_event(
                        Controller(pipeline_entry, entries), event, "Pixel",
                        "fragment", "MainPS", require_d3d11_state=False
                    )

    def test_bridge_parser_is_python36_and_matches_host_physical_signature(self):
        ast.parse(
            PARSER_PATH.read_text(encoding="utf-8"),
            filename=str(PARSER_PATH),
            feature_version=(3, 6),
        )
        ast.parse(
            BRIDGE_PATH.read_text(encoding="utf-8"),
            filename=str(BRIDGE_PATH),
            feature_version=(3, 6),
        )
        bridge_parser = load_bridge_parser()
        bridge = bridge_parser.extract_dxil_cbuffer_access_signature(
            REAL_SHAPE_DXIL
        )
        host = extract_host_signature(REAL_SHAPE_DXIL)
        self.assertEqual(bridge.to_dict(), host.to_dict())

        dynamic = REAL_SHAPE_DXIL.replace(
            b"%dx.types.Handle %18, i32 0)",
            b"%dx.types.Handle %18, i32 %dynamic)",
            1,
        )
        with self.assertRaisesRegex(
            bridge_parser.DxilAccessSignatureError, "static DXC form"
        ):
            bridge_parser.extract_dxil_cbuffer_access_signature(dynamic)

    def test_unannotated_complete_b2_uint4_is_lowered_before_apply(self):
        module = load_bridge()

        def block(bind, byte_size, variables=None):
            return types.SimpleNamespace(
                name="Arbitrary%d" % bind,
                fixedBindNumber=bind,
                fixedBindSetOrSpace=0,
                bindArraySize=1,
                byteSize=byte_size,
                bufferBacked=True,
                inlineDataBytes=False,
                compileConstants=False,
                variables=list(variables or []),
            )

        uint4_array = types.SimpleNamespace(
            name="",
            baseType="VarType.UInt",
            rows=1,
            columns=4,
            elements=33,
            arrayByteStride=16,
            matrixByteStride=0,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[],
            RowMajor=lambda: False,
        )
        reflected = reflection(b"captured")
        reflected.constantBlocks = [
            block(0, 8),
            block(1, 7572),
            block(2, 528, [types.SimpleNamespace(
                name="HumanReadableAlias",
                byteOffset=0,
                type=uint4_array,
            )]),
            block(3, 860),
        ]

        class Controller:
            def GetDisassemblyTargets(self, with_pipeline):
                return ["DXC DXIL"]

            def DisassembleShader(self, pipeline, shader_reflection, target):
                return REAL_SHAPE_DXIL.decode("utf-8")

        access, evidence = module._dxil_evidence_from_live_disassembly(
            Controller(), ResourceId(5), reflected
        )
        interface = module._compiled_shader_interface(reflected, evidence)
        b2 = next(
            item for item in interface["constantBlocks"]
            if item["bind"] == 2
        )
        self.assertEqual("ambiguous", b2["layoutCompleteness"])
        self.assertEqual("VarType.UInt", b2["fields"][0]["baseType"])
        self.assertEqual("absent", b2["layoutEvidence"]["typeAnnotation"])
        self.assertFalse(interface["interfaceComplete"])
        access_b2 = access.block(0, 2)
        assert access_b2 is not None
        self.assertEqual(
            [(312, 4, "f32")],
            [
                (item.byte_offset, item.byte_width, item.return_type)
                for item in access_b2.accesses
            ],
        )

    def test_dxil_shared_apply_reuses_original_evidence_and_checks_physical_envelope(self):
        module = load_bridge()
        baseline_blob = b"captured native DXIL"
        candidate_blob = b"candidate native DXIL"
        original_id = ResourceId(77)

        class Pipeline:
            def GetShader(self, stage):
                return original_id

            def GetShaderEntryPoint(self, stage):
                return "main"

            def GetGraphicsPipelineObject(self):
                return ResourceId(5)

        class Controller:
            def __init__(self):
                self.next_target = 100
                self.target_blobs = {}
                self.candidate_ir = REAL_SHAPE_DXIL
                self.targets = ["DXBC/DXIL", "DXC DXIL"]
                self.calls = []
                self.freed = []
                self.replacements = {}
                self.candidate_byte_size = None

            def GetAPIProperties(self):
                return types.SimpleNamespace(
                    pipelineType="GraphicsAPI.D3D12"
                )

            def SetFrameEvent(self, event, force):
                self.calls.append(("SetFrameEvent", event, force))

            def GetPipelineState(self):
                return Pipeline()

            def GetD3D12PipelineState(self):
                return types.SimpleNamespace(pipelineResourceId=ResourceId(5))

            def GetShaderEntryPoints(self, shader):
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                shader_id = int(shader)
                if shader_id == 77:
                    active = self.replacements.get(shader_id)
                    if active is None:
                        return reflection(baseline_blob)
                    shader_id = int(active)
                blob = self.target_blobs.get(shader_id)
                value = reflection(blob) if blob is not None else None
                if value is not None and self.candidate_byte_size is not None:
                    value.constantBlocks[0].byteSize = self.candidate_byte_size
                return value

            def GetTargetShaderEncodings(self):
                return ["DXIL"]

            def BuildTargetShader(self, entry, encoding, source, flags, stage):
                target = ResourceId(self.next_target)
                self.next_target += 1
                self.target_blobs[int(target)] = bytes(source)
                self.calls.append(("BuildTargetShader", int(target)))
                return target, ""

            def GetDisassemblyTargets(self, with_pipeline):
                self.calls.append(("GetDisassemblyTargets", with_pipeline))
                return list(self.targets)

            def DisassembleShader(self, pipeline, shader_reflection, target):
                self.calls.append((
                    "DisassembleShader", int(pipeline), str(target),
                    shader_reflection.rawBytes,
                ))
                if shader_reflection.rawBytes == baseline_blob:
                    return REAL_SHAPE_DXIL.decode("utf-8")
                return self.candidate_ir.decode("utf-8")

            def ReplaceTargetShaderChecked(self, original, target):
                self.calls.append((
                    "ReplaceTargetShaderChecked", int(original), int(target)
                ))
                self.replacements[int(original)] = target
                return ""

            def RemoveTargetShaderReplacementChecked(self, original):
                self.calls.append((
                    "RemoveTargetShaderReplacementChecked", int(original)
                ))
                self.replacements.pop(int(original), None)
                return ""

            def FreeTargetResource(self, target):
                self.calls.append(("FreeTargetResource", int(target)))
                self.freed.append(int(target))

        class Context:
            def __init__(self):
                self.replacements = {}

            def IsCaptureLoaded(self):
                return True

            def CurEvent(self):
                return 5

            def GetCaptureFilename(self):
                return "D:/captures/ambiguous-dxil.rdc"

            def GetResources(self):
                return [types.SimpleNamespace(resourceId=original_id)]

            def IsResourceReplaced(self, original):
                return int(original) in self.replacements

            def GetResourceReplacement(self, original):
                return self.replacements.get(int(original), ResourceId())

            def RegisterReplacement(self, original, target):
                self.replacements[int(original)] = target

            def UnregisterReplacement(self, original):
                self.replacements.pop(int(original), None)

        controller = Controller()
        context = Context()
        capture_id = "c" * 64
        capture_path = context.GetCaptureFilename()
        module._ctx = context
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(capture_path))
        }
        module._capture_session_id = "7" * 32
        module._replay = lambda callback: callback(controller)

        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.dxil"
            candidate_path.write_bytes(candidate_blob)

            def params(revision, blob=candidate_blob, path=candidate_path):
                return {
                    "candidate_path": str(path),
                    "expected_sha256": hashlib.sha256(
                        blob
                    ).hexdigest(),
                    "expected_capture_id": capture_id,
                    "event_id": 321,
                    "stage": "fragment",
                    "entry_point": "main",
                    "encoding": "dxil",
                    "expected_original_resource_id": 77,
                    "expected_original_sha256": hashlib.sha256(
                        baseline_blob
                    ).hexdigest(),
                    "workspace_id": "b" * 32,
                    "revision": revision,
                    "abi_request_id": "a" * 64,
                    # Deliberately arbitrary: the Bridge must not treat this
                    # caller report hash as physical access evidence.
                    "abi_report_sha256": "d" * 64,
                }

            applied = module._apply_shader_candidate_shared_resource(params(1))
            self.assertTrue(applied["configured"])
            self.assertEqual(int(context.replacements[77]), 100)
            disassembly_calls = [
                call for call in controller.calls
                if call[0] == "DisassembleShader"
            ]
            self.assertEqual(len(disassembly_calls), 1)
            self.assertEqual(
                [call[2] for call in disassembly_calls],
                ["DXC DXIL"],
            )
            self.assertEqual(
                [call[1] for call in disassembly_calls], [5]
            )

            supersede_blob = b"second candidate native DXIL"
            supersede_path = Path(temporary) / "candidate-rev2.dxil"
            supersede_path.write_bytes(supersede_blob)
            superseded = module._apply_shader_candidate_shared_resource(
                params(2, supersede_blob, supersede_path)
            )
            self.assertEqual(
                superseded["supersededReplacementId"], applied["replacementId"]
            )
            self.assertEqual(int(context.replacements[77]), 101)
            self.assertEqual(int(controller.replacements[77]), 101)
            self.assertIn(100, controller.freed)
            replace_index = next(
                index for index, call in enumerate(controller.calls)
                if call == ("ReplaceTargetShaderChecked", 77, 101)
            )
            free_index = next(
                index for index, call in enumerate(controller.calls)
                if call == ("FreeTargetResource", 100)
            )
            self.assertLess(replace_index, free_index)
            # Original layout evidence remains bound to the captured bytes.
            # Candidate access instructions are not a physical ABI contract.
            disassembly_calls = [
                call for call in controller.calls
                if call[0] == "DisassembleShader"
            ]
            self.assertEqual(len(disassembly_calls), 1)
            self.assertEqual([call[1] for call in disassembly_calls], [5])

            rolled_back = module._rollback_shader_candidate_shared_resource({
                "replacement_id": superseded["replacementId"],
                "expected_capture_id": capture_id,
                "workspace_id": "b" * 32,
            })
            self.assertTrue(rolled_back["cleared"])

            controller.candidate_ir = REAL_SHAPE_DXIL.replace(
                b"%20 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32",
                b"%20 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32",
            ).replace(
                b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 0",
                b"%21 = extractvalue %dx.types.CBufRet.f32 %20, 0",
            )
            # A changed CBuffer byte span is still rejected before replacement.
            controller.candidate_byte_size = 8192
            with self.assertRaisesRegex(
                RuntimeError, "physical ABI differs"
            ):
                module._apply_shader_candidate_shared_resource(params(2))
            self.assertNotIn(77, context.replacements)
            self.assertIn(102, controller.freed)
            replacement_calls = [
                call for call in controller.calls
                if call[0] == "ReplaceTargetShaderChecked"
            ]
            self.assertEqual(len(replacement_calls), 2)

            controller.candidate_ir = REAL_SHAPE_DXIL
            controller.candidate_byte_size = None
            controller.targets = ["DXBC/DXIL", "DXC DXIL (untrusted guess)"]
            with self.assertRaisesRegex(
                RuntimeError, "one exact 'DXC DXIL'"
            ):
                module._apply_shader_candidate_shared_resource(params(3))
            # Baseline identity is now derived from the same DXIL evidence as
            # the catalog before a target is built, so an untrusted printer
            # fails without allocating resource 103.
            self.assertNotIn(103, controller.target_blobs)
            self.assertEqual(controller.freed, [100, 101, 102])
            self.assertNotIn(77, context.replacements)


if __name__ == "__main__":
    unittest.main()
