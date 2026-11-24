"""Capture-bound CBuffer round-trip validation; run with qrenderdoc --python.

Default mode is a read-only probe. MODE=validate temporarily patches one reflected float
leaf at a time in a fresh replay process and always clears its own override afterwards.
This is a correctness test with GPU readbacks, not a latency benchmark.
"""

from __future__ import print_function

import hashlib
import json
import math
import os
import struct
import threading
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtWidgets


def _rid(value):
    return str(value)


def _variable_summary(constants, depth=0):
    summaries = []
    for constant in list(constants)[:8]:
        shader_type = constant.type
        entry = {"name": str(constant.name), "byteOffset": int(constant.byteOffset),
                 "bitFieldOffset": int(constant.bitFieldOffset), "bitFieldSize": int(constant.bitFieldSize),
                 "baseType": str(shader_type.baseType), "rows": int(shader_type.rows),
                 "columns": int(shader_type.columns), "elements": int(shader_type.elements),
                 "flags": int(shader_type.flags), "pointerTypeID": int(shader_type.pointerTypeID),
                 "arrayByteStride": int(shader_type.arrayByteStride),
                 "matrixByteStride": int(shader_type.matrixByteStride),
                 "memberCount": len(shader_type.members)}
        if depth < 2 and shader_type.members:
            entry["members"] = _variable_summary(shader_type.members, depth + 1)
        summaries.append(entry)
    return summaries


def _float_patches(constants, raw, block_size, prefix="", parent_offset=0, depth=0):
    """Only ordinary 32-bit float vectors and square matrix diagonals; no type guessing."""
    if depth > 16:
        return []
    patches = []
    for constant in constants:
        shader_type = constant.type
        offset = parent_offset + int(constant.byteOffset)
        name = str(constant.name)
        path = prefix + "." + name if prefix else name
        elements = int(shader_type.elements)
        if (elements < 1 or int(shader_type.flags) & ~0x000F
                or int(shader_type.pointerTypeID) != 0xFFFFFFFF
                or int(constant.bitFieldOffset) or int(constant.bitFieldSize)):
            continue
        if shader_type.members:
            if elements == 1:
                patches.extend(_float_patches(shader_type.members, raw, block_size,
                                              path, offset, depth + 1))
            continue
        if shader_type.baseType != rd.VarType.Float:
            continue
        rows, columns = int(shader_type.rows), int(shader_type.columns)
        stride = int(shader_type.arrayByteStride)
        if elements > 1 and (stride < 16 or stride % 16):
            continue
        # Bound probing cost for large View/instance arrays. Every chosen offset is reflected.
        for element in range(min(elements, 4)):
            base = offset + (element * stride if elements > 1 else 0)
            leaf_path = "%s[%d]" % (path, element) if elements > 1 else path
            if rows == 1 and 1 <= columns <= 4:
                offsets = [base + component * 4 for component in range(columns)]
                kind = "float-vector-half-or-quarter"
            elif rows == columns and rows in (3, 4) and int(shader_type.matrixByteStride) >= rows * 4:
                # Diagonal addresses are the same for row-major and column-major square matrices.
                offsets = [base + component * (int(shader_type.matrixByteStride) + 4)
                           for component in range(min(rows, 3))]
                kind = "matrix-first-three-diagonal-half"
            else:
                continue
            if any(position < 0 or position % 4 or position + 4 > min(len(raw), block_size)
                   for position in offsets):
                continue
            before = [struct.unpack_from("<f", raw, position)[0] for position in offsets]
            if any(not math.isfinite(value) or abs(value) > 1.0e8 for value in before):
                continue
            if kind.startswith("matrix"):
                after = [value * 0.5 for value in before]
            else:
                after = [value * 0.5 if abs(value) > 1.0e-6 else 0.25 for value in before]
            if all(a == b for a, b in zip(before, after)):
                continue
            data = bytearray(raw)
            for position, value in zip(offsets, after):
                struct.pack_into("<f", data, position, value)
            patches.append({"path": leaf_path, "kind": kind, "offsets": offsets,
                            "before": before, "after": after, "data": bytes(data)})
    return patches


class _Validator(object):
    def __init__(self):
        self.application = QtWidgets.QApplication.instance()
        self.result = {"schema": "renderdoc-eid-cbuffer-validation/v2", "ok": False,
                       "candidates": [], "attempts": []}
        self.timer = None
        self.finished = False
        self.original = None
        self.phase = "startup"
        self.output_path = os.environ.get("RENDERDOC_EID_CBUFFER_OUTPUT", "")

    def start(self):
        try:
            self.event = int(os.environ["RENDERDOC_EID_CBUFFER_EID"])
            self.mode = os.environ.get("RENDERDOC_EID_CBUFFER_MODE", "probe")
            if self.event <= 0 or self.mode not in ("probe", "validate"):
                raise ValueError("EID must be positive and MODE must be probe or validate")
            if self.mode == "validate" and os.environ.get("RENDERDOC_EID_TIMING") != "1":
                raise RuntimeError("Validation needs RENDERDOC_EID_TIMING=1 before process startup")
            self.max_candidates = int(os.environ.get("RENDERDOC_EID_CBUFFER_MAX_CANDIDATES", "24"))
            self.native_proof_path = os.environ.get("RENDERDOC_EID_CBUFFER_NATIVE_F32_PROOF", "")
            self.native_proof = None
            if self.native_proof_path:
                with open(self.native_proof_path, "r", encoding="utf-8") as source:
                    self.native_proof = json.load(source)
                if self.native_proof.get("schema") != "renderdoc-eid-native-f32-proof/v1":
                    raise ValueError("Unsupported native F32 evidence schema")
            self.read_budget = int(os.environ.get("RENDERDOC_EID_CBUFFER_MAX_READ_MIB", "256")) * 1024 * 1024
            if self.max_candidates < 1 or self.read_budget < 1:
                raise ValueError("Candidate and readback limits must be positive")
            self.log_path = os.path.join(self.application.applicationDirPath(), "eid_timing_driver.log")
            self.result.update({"eventId": self.event, "mode": self.mode,
                                "buildLabel": os.environ.get("RENDERDOC_EID_CBUFFER_BUILD_LABEL", "unspecified"),
                                "driverTimingLog": self.log_path,
                                "realReplayRequired": True, "allBoundColorTargetsRequired": True,
                                "hashEvidence": "raw bytes of actual bound color render-target subresources, not a screenshot of the Texture Viewer"})
            self.timer = QtCore.QTimer(self.application)
            self.timer.setInterval(20)
            self.timer.timeout.connect(self.tick)
            self.deadline = time.monotonic() + 180.0
            self.phase = "loading"
            capture = os.environ.get("RENDERDOC_EID_CBUFFER_CAPTURE", "")
            if not pyrenderdoc.IsCaptureLoaded() and capture:
                pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            self.timer.start()
        except Exception as exc:
            self.finish(exc)

    def dispatch(self, name, operation):
        self.done = threading.Event()
        self.worker_error = None
        self.worker_traceback = None

        def work(controller):
            try:
                operation(controller)
            except Exception as exc:
                self.worker_error = "%s: %s" % (type(exc).__name__, exc)
                self.worker_traceback = traceback.format_exc()
            finally:
                self.done.set()

        self.phase = name
        self.deadline = time.monotonic() + 180.0
        pyrenderdoc.Replay().AsyncInvoke(work, "eid-cbuffer-" + name)

    def prepare(self):
        self.original = (int(pyrenderdoc.CurSelectedEvent()), int(pyrenderdoc.CurEvent()))
        action = pyrenderdoc.GetAction(self.event)
        if action is None or not (action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch)):
            raise ValueError("Choose an actual graphics Draw/MeshDispatch EID with color render targets")
        self.indirect = bool(action.flags & rd.ActionFlags.Indirect)
        alternatives = []

        def visit(actions):
            for candidate in actions:
                if candidate.flags & rd.ActionFlags.Drawcall and int(candidate.eventId) != self.event:
                    alternatives.append(int(candidate.eventId))
                visit(candidate.children)

        visit(pyrenderdoc.CurRootActions())
        self.away_event = int(os.environ.get("RENDERDOC_EID_CBUFFER_AWAY_EID",
                                             str(alternatives[-1] if alternatives else 0)))
        if self.away_event <= 0 or self.away_event == self.event or pyrenderdoc.GetAction(self.away_event) is None:
            raise ValueError("A distinct valid AWAY_EID is required for the real A-to-B-to-A replay check")
        self.result["awayEventId"] = self.away_event
        pyrenderdoc.SetEventID([], self.event, self.event, True)
        pyrenderdoc.ShowTextureViewer()
        self.viewer = pyrenderdoc.GetTextureViewer()
        self.viewer_widget = self.viewer.Widget()
        self.render_widget = self.viewer_widget.findChild(QtWidgets.QWidget, "render")
        self.dispatch("probe", self.probe)

    def probe(self, controller):
        props = controller.GetAPIProperties()
        if props.pipelineType not in (rd.GraphicsAPI.D3D11, rd.GraphicsAPI.D3D12) or props.remoteReplay:
            raise RuntimeError("This validation requires a local D3D11 or D3D12 replay")
        if int(controller.GetShaderReplacementGeneration(0)) != 1:
            raise RuntimeError("Use a fresh process without existing shader/resource/CBuffer edits; this API has no override getter for backing up someone else's edit")
        self.api = props.pipelineType
        self.result["api"] = str(self.api)
        pipe = controller.GetPipelineState()
        textures = list(controller.GetTextures())
        self.targets = []
        used_budget = 0
        for index, binding in enumerate(pipe.GetOutputTargets()):
            matches = [texture for texture in textures if texture.resourceId == binding.resource]
            if binding.resource == rd.ResourceId.Null():
                continue
            if not matches:
                raise RuntimeError("A bound color target is missing from the texture descriptions")
            texture = matches[0]
            mip, slice_index = int(binding.firstMip), int(binding.firstSlice)
            estimate = max(1, int(texture.width) >> mip) * max(1, int(texture.height) >> mip) * 16
            if used_budget + estimate > self.read_budget:
                raise RuntimeError("All bound color targets must fit the readback budget; increase MAX_READ_MIB")
            used_budget += estimate
            sub = rd.Subresource(mip, slice_index, 0)
            self.targets.append({"resource": binding.resource, "sub": sub, "slot": index,
                                 "description": {"slot": index, "resource": _rid(binding.resource),
                                                 "mip": mip, "slice": slice_index, "sample": 0,
                                                 "width": int(texture.width), "height": int(texture.height)}})
        if not self.targets:
            raise RuntimeError("No actual bound color render target fits the readback budget")
        self.result["targets"] = [target["description"] for target in self.targets]
        self.candidates = []
        self.result["excludedBlocks"] = []
        self.result["stages"] = []
        stages = [rd.ShaderStage.Pixel, rd.ShaderStage.Vertex, rd.ShaderStage.Mesh,
                  rd.ShaderStage.Geometry, rd.ShaderStage.Domain, rd.ShaderStage.Hull]
        buffers = list(controller.GetBuffers())
        for stage in stages:
            reflection = pipe.GetShaderReflection(stage)
            shader = pipe.GetShader(stage)
            stage_info = {"stage": str(stage), "shader": _rid(shader),
                          "reflectionAvailable": reflection is not None, "blocks": []}
            self.result["stages"].append(stage_info)
            if reflection is None or shader == rd.ResourceId.Null():
                stage_info["skipReason"] = "No bound shader or shader reflection"
                continue
            stage_info["encoding"] = str(reflection.encoding)
            if (os.environ.get("RENDERDOC_EID_CBUFFER_DUMP_SHADERS") == "1"
                    and stage in (rd.ShaderStage.Pixel, rd.ShaderStage.Vertex)):
                if not self.output_path:
                    raise ValueError("DUMP_SHADERS needs an OUTPUT path for the adjacent disassembly files")
                disassembly_path = self.output_path + "." + str(stage).split(".")[-1] + ".txt"
                disassembly = controller.DisassembleShader(pipe.GetGraphicsPipelineObject(), reflection, "")
                with open(disassembly_path, "w", encoding="utf-8") as output:
                    output.write(str(disassembly))
                stage_info["disassemblyPath"] = disassembly_path
            stage_info["blockCount"] = len(reflection.constantBlocks)
            for block_index, block in enumerate(reflection.constantBlocks):
                block_info = {"blockIndex": block_index, "name": str(block.name),
                              "byteSize": int(block.byteSize), "bufferBacked": bool(block.bufferBacked),
                              "inlineDataBytes": bool(block.inlineDataBytes),
                              "compileConstants": bool(block.compileConstants),
                              "bindArraySize": int(block.bindArraySize),
                              "slot": int(block.fixedBindNumber), "space": int(block.fixedBindSetOrSpace),
                              "variableCount": len(block.variables),
                              "variables": _variable_summary(block.variables)}
                stage_info["blocks"].append(block_info)
                try:
                    # Record the observed descriptor even for an unsupported reflected block.
                    binding = pipe.GetConstantBlock(stage, block_index, 0).descriptor
                    block_info["binding"] = {"resource": _rid(binding.resource),
                                             "byteOffset": int(binding.byteOffset),
                                             "byteSize": int(binding.byteSize)}
                    if (not block.bufferBacked or block.inlineDataBytes or block.compileConstants
                            or int(block.bindArraySize) != 1):
                        block_info["skipReason"] = "Not one ordinary buffer-backed runtime CBuffer"
                        continue
                    slot, space = int(block.fixedBindNumber), int(block.fixedBindSetOrSpace)
                    if binding.resource == rd.ResourceId.Null():
                        block_info["skipReason"] = "Unbound reflected constant block"
                        continue
                    offset, size = int(binding.byteOffset), int(binding.byteSize)
                    root_cbv = False
                    if self.api == rd.GraphicsAPI.D3D12:
                        for parameter in controller.GetD3D12PipelineState().rootSignature.parameters:
                            descriptor = parameter.descriptor
                            if (int(parameter.visibility) & (1 << int(stage))
                                    and int(parameter.reg) == slot and int(parameter.space) == space
                                    and "constantbuffer" in str(descriptor.type).replace("_", "").lower()
                                    and descriptor.resource == binding.resource
                                    and int(descriptor.byteOffset) == offset):
                                root_cbv = True
                                break
                        if root_cbv:
                            if self.indirect:
                                raise ValueError("ExecuteIndirect root CBV is unsupported")
                            size = (int(block.byteSize) + 15) & ~15
                        if offset % 256:
                            raise ValueError("Unaligned D3D12 CBV offset")
                    elif space != 0 or slot < 0 or slot >= 14:
                        block_info["skipReason"] = "Unsupported D3D11 slot or space"
                        continue
                    matching_buffers = [buffer for buffer in buffers if buffer.resourceId == binding.resource]
                    if (not matching_buffers or offset < 0 or size <= 0 or size > 65536
                            or offset % 16 or size % 16 or offset + size > int(matching_buffers[0].length)):
                        raise ValueError("The complete reflected binding window is unsupported")
                    raw = bytes(controller.GetBufferData(binding.resource, offset, size))
                    if len(raw) != size:
                        raise RuntimeError("CBuffer readback returned a short window")
                    patches = _float_patches(block.variables, raw, int(block.byteSize))
                    proof = self.native_proof
                    if (proof is not None and str(stage) == proof["stage"]
                            and slot == int(proof["slot"]) and space == int(proof["registerSpace"])):
                        # This is an explicit, reviewed single-load fixture, not reconstructed
                        # Float reflection or a heuristic scan of untyped constant bytes.
                        if (_rid(shader) != proof["shaderResource"]
                                or _rid(binding.resource) != proof["bufferResource"]
                                or offset != int(proof["bindingByteOffset"])
                                or size != int(proof["bindingByteSize"])):
                            raise ValueError("Native proof does not match the actual shader/CBV binding")
                        disassembly = str(controller.DisassembleShader(
                            pipe.GetGraphicsPipelineObject(), reflection, ""))
                        observed_lines = set(line.strip() for line in disassembly.splitlines())
                        if "; shader hash: " + proof["shaderHash"] not in observed_lines:
                            raise ValueError("Native shader hash differs from the reviewed evidence")
                        if not all(line.strip() in observed_lines for line in proof["evidenceLines"]):
                            raise ValueError("The exact reviewed F32 load/extract/output evidence changed")
                        position = int(proof["byteOffset"])
                        if (position != int(proof["loadByteOffset"]) + int(proof["component"]) * 4
                                or int(proof["component"]) not in range(4)
                                or position % 4 or position < 0
                                or position + 4 > min(len(raw), int(block.byteSize))):
                            raise ValueError("Native F32 evidence points outside the validated CBV window")
                        before = struct.unpack_from("<f", raw, position)[0]
                        if not math.isfinite(before):
                            raise ValueError("The reviewed native float is not finite")
                        after = before * 0.5 if 0.0 < before * 0.125 < 1.0 else 1.0
                        data = bytearray(raw)
                        struct.pack_into("<f", data, position, after)
                        patches.append({"path": "native:b%d.space%d.byte%d" % (slot, space, position),
                                        "kind": "reviewed-native-f32-single-value", "offsets": [position],
                                        "before": [before], "after": [after], "data": bytes(data),
                                        "nativeEvidence": {"path": self.native_proof_path,
                                                           "shaderHash": proof["shaderHash"],
                                                           "evidenceLines": proof["evidenceLines"]}})
                    block_info.update({"validatedWindowSize": size, "rootCBV": root_cbv,
                                       "floatCandidateCount": len(patches)})
                    if not patches:
                        block_info["skipReason"] = "No finite float vector/matrix passed the reflected layout and bounds filters"
                    for patch in patches:
                        candidate = {"stage": stage, "shader": shader, "buffer": binding.resource,
                                     "slot": slot, "space": space, "offset": offset, "size": size,
                                     "raw": raw, "patch": patch,
                                     "description": {"stage": str(stage), "blockIndex": block_index,
                                                     "blockName": str(block.name), "slot": slot,
                                                     "registerSpace": space, "shader": _rid(shader),
                                                     "buffer": _rid(binding.resource), "byteOffset": offset,
                                                     "byteSize": size, "rootCBV": root_cbv}}
                        candidate["description"].update({key: value for key, value in patch.items() if key != "data"})
                        self.candidates.append(candidate)
                except Exception as exc:
                    block_info["skipReason"] = str(exc)
                    self.result["excludedBlocks"].append({"stage": str(stage), "blockIndex": block_index,
                                                           "reason": str(exc)})
        # Prefer a definite, bounded matrix scale, then nonzero vectors. No semantic variable names
        # or hard-coded byte offsets influence this selection.
        self.candidates.sort(key=lambda item: ("nativeEvidence" not in item["patch"],
                                               not item["patch"]["kind"].startswith("matrix"),
                                               not any(item["patch"]["before"])))
        self.result["candidateCount"] = len(self.candidates)
        for index, candidate in enumerate(self.candidates):
            candidate["description"]["candidateIndex"] = index
        self.result["candidates"] = [candidate["description"] for candidate in self.candidates[:64]]
        if not self.candidates:
            raise RuntimeError("No supported finite reflected float candidate was found")
        self.baseline = self.hash_targets(controller)
        controller.SetFrameEvent(self.event, True)
        repeated = self.hash_targets(controller)
        self.result["baselineHashes"] = self.baseline
        self.result["baselineRepeatedHashes"] = repeated
        if repeated != self.baseline:
            raise RuntimeError("Baseline target hashes are not deterministic across two forced replays")

    def hash_targets(self, controller):
        hashes = {}
        for target in self.targets:
            raw = bytes(controller.GetTextureData(target["resource"], target["sub"]))
            if not raw:
                raise RuntimeError("A render-target readback returned no bytes")
            hashes[str(target["slot"])] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        fatal = controller.GetFatalErrorStatus()
        if fatal.code != rd.ResultCode.Succeeded:
            raise RuntimeError(str(fatal))
        return hashes

    def log_offset(self):
        return os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0

    def trace_since(self, offset):
        with open(self.log_path, "rb") as source:
            source.seek(offset)
            lines = source.read().decode("utf-8", "replace").splitlines()
        return [line for line in lines if "api=Scrub" in line or "api=VirtualMaterialize" in line
                or "api=MaterializeEvent" in line]

    def real_scrub(self, controller, event):
        """Require a fresh real replay even when revisiting the same event."""
        start = self.log_offset()
        controller.ScrubFrameEvent(event)
        controller.CommitScrubFrameEvent(event)
        trace = self.trace_since(start)
        if not any("api=ScrubReplay eid=%d " % event in line for line in trace):
            raise RuntimeError("EID %d did not emit a new real ScrubReplay record" % event)
        if any(tag in line for line in trace for tag in
               ("api=ScrubSnapshotHit ", "api=ScrubCommitCacheHit ", "api=VirtualMaterialize ")):
            raise RuntimeError("Real EID validation encountered a virtual/snapshot shortcut")
        return trace

    def identity_control(self, controller, candidate, control):
        control["beforeSetScrubTrace"] = self.real_scrub(controller, self.event)
        control["generationBefore"] = int(controller.GetShaderReplacementGeneration(0))
        try:
            error = controller.SetEventCBufferOverride(
                self.event, candidate["stage"], candidate["slot"], candidate["shader"],
                candidate["buffer"], candidate["offset"], candidate["size"],
                candidate["raw"], candidate["space"])
            control["setterError"] = str(error or "")
            if error:
                raise RuntimeError("Identity-shadow setter failed: " + str(error))
            control["generationAfterSet"] = int(controller.GetShaderReplacementGeneration(0))
            if control["generationAfterSet"] <= control["generationBefore"]:
                raise RuntimeError("Identity shadow did not advance the output generation")
            control["afterSetScrubTrace"] = self.real_scrub(controller, self.event)
            control["shadowHashes"] = self.hash_targets(controller)
            control["shadowMatchesBaseline"] = control["shadowHashes"] == self.baseline
            control["capturedBufferUnchanged"] = bytes(controller.GetBufferData(
                candidate["buffer"], candidate["offset"], candidate["size"])) == candidate["raw"]
            if not control["capturedBufferUnchanged"]:
                raise RuntimeError("Identity shadow changed the captured CBuffer resource")
            control["changedTargetSlots"] = [slot for slot in self.baseline
                                              if control["shadowHashes"][slot] != self.baseline[slot]]
            if not control["shadowMatchesBaseline"]:
                raise RuntimeError("Uploading the unchanged CBuffer window changed a render target")
        except Exception as exc:
            control["error"] = str(exc)
            raise
        finally:
            controller.ClearEventCBufferOverride(self.event, candidate["stage"],
                                                candidate["slot"], candidate["space"])
            control["generationAfterClear"] = int(controller.GetShaderReplacementGeneration(0))
            control["afterClearScrubTrace"] = self.real_scrub(controller, self.event)
            control["restoredHashes"] = self.hash_targets(controller)
            control["clearMatchesBaseline"] = control["restoredHashes"] == self.baseline
            if not control["clearMatchesBaseline"]:
                raise RuntimeError("Clearing the identity shadow did not restore baseline target hashes")
            if ("generationAfterSet" in control
                    and control["generationAfterClear"] <= control["generationAfterSet"]):
                raise RuntimeError("Clearing the identity shadow did not advance the output generation")

    def validate(self, controller):
        candidates = self.candidates[:self.max_candidates]
        selected = os.environ.get("RENDERDOC_EID_CBUFFER_CANDIDATE", "").strip()
        if selected:
            candidates = [self.candidates[int(selected)]]
        for candidate in candidates:
            description = candidate["description"]
            attempt = {"candidateIndex": description["candidateIndex"], "path": description["path"]}
            self.result["attempts"].append(attempt)
            attempt["identityControl"] = {}
            self.identity_control(controller, candidate, attempt["identityControl"])
            touched = False
            try:
                # Every leg materialises the real event, including the return to A. The setter
                # below operates directly on A's live shader/CBV binding, without image caching.
                attempt["firstATrace"] = self.real_scrub(controller, self.event)
                attempt["awayTrace"] = self.real_scrub(controller, self.away_event)
                away_shader = controller.GetPipelineState().GetShader(candidate["stage"])
                attempt["awayShader"] = _rid(away_shader)
                attempt["differentAwayShader"] = away_shader != candidate["shader"]
                attempt["returnATrace"] = self.real_scrub(controller, self.event)
                live_pipe = controller.GetPipelineState()
                live_shader = live_pipe.GetShader(candidate["stage"])
                live_binding = live_pipe.GetConstantBlock(
                    candidate["stage"], description["blockIndex"], 0).descriptor
                attempt["bindingBeforeSet"] = {
                    "shader": _rid(live_shader), "buffer": _rid(live_binding.resource),
                    "byteOffset": int(live_binding.byteOffset), "byteSize": int(live_binding.byteSize)}
                if (live_shader != candidate["shader"] or live_binding.resource != candidate["buffer"]
                        or int(live_binding.byteOffset) != candidate["offset"]):
                    raise RuntimeError("Real A replay did not restore the candidate shader/CBV binding")
                attempt["beforeSetHashes"] = self.hash_targets(controller)
                if attempt["beforeSetHashes"] != self.baseline:
                    raise RuntimeError("A-to-B-to-A did not restore all baseline render-target hashes")
                attempt["generationBefore"] = int(controller.GetShaderReplacementGeneration(0))
                start = self.log_offset()
                # No event switch occurs between observing A's actual binding and applying its edit.
                touched = True
                error = controller.SetEventCBufferOverride(
                    self.event, candidate["stage"], candidate["slot"], candidate["shader"],
                    candidate["buffer"], candidate["offset"], candidate["size"],
                    candidate["patch"]["data"], candidate["space"])
                attempt["setterTrace"] = self.trace_since(start)
                attempt["setterError"] = str(error or "")
                if error:
                    continue
                attempt["generationAfterSet"] = int(controller.GetShaderReplacementGeneration(0))
                if attempt["generationAfterSet"] <= attempt["generationBefore"]:
                    raise RuntimeError("Successful CBuffer mutation did not advance the output generation")
                if any(tag in line for line in attempt["setterTrace"] for tag in
                       ("api=VirtualMaterialize ", "api=MaterializeEvent ", "api=ScrubSnapshotHit ")):
                    raise RuntimeError("Direct setter unexpectedly needed to restore the event after observing A's binding")
                attempt["afterSetScrubTrace"] = self.real_scrub(controller, self.event)
                attempt["modifiedHashes"] = self.hash_targets(controller)
                attempt["changedTargetSlots"] = [slot for slot in self.baseline
                                                 if attempt["modifiedHashes"][slot] != self.baseline[slot]]
                attempt["capturedBufferUnchanged"] = bytes(controller.GetBufferData(
                    candidate["buffer"], candidate["offset"], candidate["size"])) == candidate["raw"]
                if not attempt["capturedBufferUnchanged"]:
                    raise RuntimeError("The captured CBuffer resource was changed instead of using a shadow")
            finally:
                if touched:
                    controller.ClearEventCBufferOverride(self.event, candidate["stage"],
                                                        candidate["slot"], candidate["space"])
                    attempt["generationAfterClear"] = int(controller.GetShaderReplacementGeneration(0))
                    attempt["afterClearScrubTrace"] = self.real_scrub(controller, self.event)
                    attempt["restoredHashes"] = self.hash_targets(controller)
                    if attempt["restoredHashes"] != self.baseline:
                        raise RuntimeError("Clearing the CBuffer override did not restore all baseline target hashes")
                    attempt["capturedBufferUnchangedAfterClear"] = bytes(controller.GetBufferData(
                        candidate["buffer"], candidate["offset"], candidate["size"])) == candidate["raw"]
                    if not attempt["capturedBufferUnchangedAfterClear"]:
                        raise RuntimeError("The captured CBuffer resource differs after clearing the override")
                    if ("generationAfterSet" in attempt
                            and attempt["generationAfterClear"] <= attempt["generationAfterSet"]):
                        raise RuntimeError("Clearing the edit did not advance the output generation")
            if attempt.get("changedTargetSlots"):
                self.result["validatedCandidate"] = description
                self.result["ok"] = True
                return
        raise RuntimeError("No candidate produced an actual render-target hash change; inspect probe output or choose another draw/candidate")

    def tick(self):
        try:
            if self.phase == "loading":
                if pyrenderdoc.IsCaptureLoaded():
                    self.prepare()
                elif time.monotonic() > self.deadline:
                    raise RuntimeError("Capture load timed out")
            elif self.phase in ("probe", "validate") and self.done.is_set():
                if self.worker_error:
                    self.result["workerTraceback"] = self.worker_traceback
                    raise RuntimeError(self.worker_error)
                if self.phase == "probe" and self.mode == "validate":
                    # Bind a real output of the selected draw; hashes still cover every bound RT.
                    target = self.targets[0]
                    self.viewer.ViewTexture(target["resource"], rd.CompType.Typeless, True)
                    self.viewer.SetSelectedSubresource(target["sub"])
                    self.viewer.SetTextureOverlay(rd.DebugOverlay.NoOverlay)
                    self.channels_widget = self.viewer_widget.findChild(QtWidgets.QComboBox, "channels")
                    self.phase = "visible"
                    self.visible_after = time.monotonic() + 1.0
                else:
                    if self.mode == "probe":
                        self.result["ok"] = True
                        self.result["validationPerformed"] = False
                    else:
                        self.result["validationPerformed"] = True
                    self.finish()
            elif self.phase == "visible" and time.monotonic() >= self.visible_after:
                if (self.render_widget is None or not self.render_widget.isVisible()
                        or self.channels_widget is None or self.channels_widget.currentIndex() != 0):
                    raise RuntimeError("Keep the locked Texture Viewer visible in ordinary RGBA mode")
                self.dispatch("validate", self.validate)
        except Exception as exc:
            self.finish(exc)

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        if self.timer is not None:
            self.timer.stop()
        if error is not None:
            self.result.update({"ok": False, "error": "%s: %s" % (type(error).__name__, error),
                                "failurePhase": self.phase, "traceback": traceback.format_exc()})
        if self.original is not None and pyrenderdoc.IsCaptureLoaded():
            try:
                pyrenderdoc.SetEventID([], self.original[0], self.original[1], False)
            except Exception as exc:
                self.result["restoreError"] = str(exc)
                self.result["ok"] = False
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("RENDERDOC_EID_CBUFFER=" + encoded)
        if self.output_path:
            with open(self.output_path, "w", encoding="utf-8") as output:
                output.write(encoded)
        if os.environ.get("RENDERDOC_EID_CBUFFER_QUIT") == "1":
            self.application.closeAllWindows()
            QtCore.QTimer.singleShot(0, lambda: self.application.exit(0))


runner = _Validator()
runner.application._eid_cbuffer_validator = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
