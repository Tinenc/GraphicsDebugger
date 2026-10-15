"""
RenderDoc API wrapper — manages capture loading and ReplayController lifecycle.

This module provides a safe, reusable abstraction over the raw renderdoc module,
handling initialization, capture file management, and resource cleanup.
"""

from __future__ import annotations

import os
import sys
import base64
import struct
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("renderdoc-mcp")

# ---------------------------------------------------------------------------
# Lazy-import renderdoc to allow the server to start even when the module
# is not yet on the path.  Users configure RENDERDOC_MODULE_PATH at runtime.
# ---------------------------------------------------------------------------
_rd = None


def _ensure_rd():
    """Import ``renderdoc`` on first use, respecting RENDERDOC_MODULE_PATH."""
    global _rd
    if _rd is not None:
        return _rd

    module_path = os.environ.get("RENDERDOC_MODULE_PATH", "")
    if module_path:
        abs_path = os.path.abspath(module_path)
        if abs_path not in sys.path:
            sys.path.insert(0, abs_path)
        # Windows Python 3.8+ requires explicit DLL directory
        if sys.platform == "win32" and sys.version_info >= (3, 8):
            os.add_dll_directory(abs_path)
        os.environ["PATH"] = abs_path + os.pathsep + os.environ.get("PATH", "")

    import renderdoc as rd  # type: ignore[import-untyped]

    _rd = rd
    return rd


# ---------------------------------------------------------------------------
# Session: wraps one loaded .rdc capture
# ---------------------------------------------------------------------------

class CaptureSession:
    """Wraps a single RenderDoc capture file + ReplayController."""

    def __init__(self, path: str):
        rd = _ensure_rd()
        self.rd = rd
        self.path = path

        rd.InitialiseReplay(rd.GlobalEnvironment(), [])

        self._cap = rd.OpenCaptureFile()
        result = self._cap.OpenFile(path, "", None)
        if result != rd.ResultCode.Succeeded:
            self._cap.Shutdown()
            rd.ShutdownReplay()
            raise RuntimeError(f"Cannot open capture '{path}': {result}")

        if not self._cap.LocalReplaySupport():
            self._cap.Shutdown()
            rd.ShutdownReplay()
            raise RuntimeError(f"Capture '{path}' cannot be replayed on this platform")

        result, self.ctrl = self._cap.OpenCapture(rd.ReplayOptions(), None)
        if result != rd.ResultCode.Succeeded:
            self._cap.Shutdown()
            rd.ShutdownReplay()
            raise RuntimeError(f"Cannot start replay for '{path}': {result}")

        logger.info("Opened capture: %s", path)

    # --- lifecycle ---

    def close(self):
        if self.ctrl:
            self.ctrl.Shutdown()
            self.ctrl = None
        if self._cap:
            self._cap.Shutdown()
            self._cap = None
        self.rd.ShutdownReplay()
        logger.info("Closed capture: %s", self.path)

    # --- information ---

    def get_api_properties(self) -> dict:
        props = self.ctrl.GetAPIProperties()
        return {
            "pipelineType": str(props.pipelineType),
            "localRenderer": str(props.localRenderer),
            "degraded": props.degraded,
        }

    def get_frame_info(self) -> dict:
        fi = self.ctrl.GetFrameInfo()
        return {
            "frameNumber": fi.frameNumber,
            "captureTime": fi.captureTime,
            "fileOffset": fi.fileOffset,
            "stats": {
                "recorded": {
                    "calls": fi.stats.recorded.calls,
                    "sets": fi.stats.recorded.sets,
                    "draws": fi.stats.recorded.draws,
                    "dispatches": fi.stats.recorded.dispatches,
                },
            },
        }

    # --- actions (drawcalls) ---

    def _flatten_actions(self, actions, depth=0) -> list[dict]:
        result = []
        for a in actions:
            entry = {
                "eventId": a.eventId,
                "actionId": a.actionId,
                "name": a.customName if a.customName else f"Action {a.actionId}",
                "flags": int(a.flags),
                "numIndices": a.numIndices,
                "numInstances": a.numInstances,
                "depth": depth,
            }
            if a.outputs:
                entry["outputs"] = [int(o) for o in a.outputs if int(o) != 0]
            if a.depthOut and int(a.depthOut) != 0:
                entry["depthOutput"] = int(a.depthOut)
            result.append(entry)
            if a.children:
                result.extend(self._flatten_actions(a.children, depth + 1))
        return result

    def get_actions(self) -> list[dict]:
        return self._flatten_actions(self.ctrl.GetRootActions())

    # --- resources ---

    def get_textures(self) -> list[dict]:
        textures = self.ctrl.GetTextures()
        return [
            {
                "resourceId": int(t.resourceId),
                "name": t.name,
                "width": t.width,
                "height": t.height,
                "depth": t.depth,
                "mips": t.mips,
                "arraysize": t.arraysize,
                "format": str(t.format),
                "dimension": t.dimension,
                "type": str(t.type),
                "msQual": t.msQual,
                "msSamp": t.msSamp,
                "byteSize": t.byteSize,
                "creationFlags": int(t.creationFlags),
            }
            for t in textures
        ]

    def get_buffers(self) -> list[dict]:
        buffers = self.ctrl.GetBuffers()
        return [
            {
                "resourceId": int(b.resourceId),
                "name": b.name,
                "length": b.length,
                "creationFlags": int(b.creationFlags),
            }
            for b in buffers
        ]

    def get_resources(self) -> list[dict]:
        resources = self.ctrl.GetResources()
        return [
            {
                "resourceId": int(r.resourceId),
                "name": r.name,
                "type": str(r.type),
            }
            for r in resources
        ]

    def get_resource_usage(self, resource_id: int) -> list[dict]:
        rd = self.rd
        rid = rd.ResourceId()
        rid.id = resource_id  # type: ignore[attr-defined]
        usages = self.ctrl.GetUsage(rid)
        return [
            {
                "eventId": u.eventId,
                "usage": str(u.usage),
            }
            for u in usages
        ]

    # --- pipeline state ---

    def set_event(self, event_id: int):
        self.ctrl.SetFrameEvent(event_id, True)

    def get_pipeline_state(self) -> dict:
        ps = self.ctrl.GetPipelineState()
        result: dict[str, Any] = {}

        # Vertex shader
        vs = ps.GetShader(self.rd.ShaderStage.Vertex)
        result["vertexShader"] = int(vs) if vs and int(vs) != 0 else None

        # Fragment / pixel shader
        fs = ps.GetShader(self.rd.ShaderStage.Fragment)
        result["fragmentShader"] = int(fs) if fs and int(fs) != 0 else None

        # Geometry
        gs = ps.GetShader(self.rd.ShaderStage.Geometry)
        result["geometryShader"] = int(gs) if gs and int(gs) != 0 else None

        # Compute
        cs = ps.GetShader(self.rd.ShaderStage.Compute)
        result["computeShader"] = int(cs) if cs and int(cs) != 0 else None

        # Output targets
        targets = ps.GetOutputTargets()
        result["renderTargets"] = [int(t.resource) for t in targets if int(t.resource) != 0]

        # Depth
        depth = ps.GetDepthTarget()
        result["depthTarget"] = int(depth.resource) if depth and int(depth.resource) != 0 else None

        # Viewports
        viewports = ps.GetViewport(0)
        if viewports:
            result["viewport"] = {
                "x": viewports.x,
                "y": viewports.y,
                "width": viewports.width,
                "height": viewports.height,
            }

        return result

    # --- shader inspection ---

    def get_shader_reflection(self, pipeline_id: int, shader_id: int) -> dict:
        rd = self.rd
        pid = rd.ResourceId()
        pid.id = pipeline_id  # type: ignore
        sid = rd.ResourceId()
        sid.id = shader_id  # type: ignore

        entries = self.ctrl.GetShaderEntryPoints(sid)
        if not entries:
            return {"error": "No entry points found"}

        entry = entries[0]
        refl = self.ctrl.GetShader(pid, sid, entry)
        if not refl:
            return {"error": "No reflection data"}

        result: dict[str, Any] = {
            "entryPoint": entry.name,
            "stage": str(refl.stage),
            "debugInfo": {
                "compileFlags": str(refl.debugInfo.compileFlags) if refl.debugInfo else None,
            },
        }

        # Inputs
        result["inputSignature"] = [
            {
                "varName": sig.varName,
                "semanticName": sig.semanticName,
                "semanticIndex": sig.semanticIndex,
                "compType": str(sig.compType),
                "compCount": sig.compCount,
            }
            for sig in refl.inputSignature
        ]

        # Outputs
        result["outputSignature"] = [
            {
                "varName": sig.varName,
                "semanticName": sig.semanticName,
                "semanticIndex": sig.semanticIndex,
                "compType": str(sig.compType),
                "compCount": sig.compCount,
            }
            for sig in refl.outputSignature
        ]

        # Constant buffers
        result["constantBuffers"] = [
            {
                "name": cb.name,
                "byteSize": cb.byteSize,
                "bindPoint": cb.fixedBindNumber,
                "variables": [
                    {"name": v.name, "type": str(v.type.descriptor.type), "rows": v.type.descriptor.rows, "columns": v.type.descriptor.columns}
                    for v in cb.variables
                ],
            }
            for cb in refl.constantBlocks
        ]

        # Samplers
        result["samplers"] = [
            {"name": s.name, "bindPoint": s.fixedBindNumber}
            for s in refl.samplers
        ]

        # Read-only resources
        result["readOnlyResources"] = [
            {
                "name": r.name,
                "bindPoint": r.fixedBindNumber,
                "type": str(r.resType),
                "isTexture": r.isTexture,
            }
            for r in refl.readOnlyResources
        ]

        # Read-write resources
        result["readWriteResources"] = [
            {
                "name": r.name,
                "bindPoint": r.fixedBindNumber,
                "type": str(r.resType),
                "isTexture": r.isTexture,
            }
            for r in refl.readWriteResources
        ]

        return result

    def disassemble_shader(self, pipeline_id: int, shader_id: int, target: str = "") -> str:
        rd = self.rd
        pid = rd.ResourceId()
        pid.id = pipeline_id  # type: ignore
        sid = rd.ResourceId()
        sid.id = shader_id  # type: ignore

        entries = self.ctrl.GetShaderEntryPoints(sid)
        if not entries:
            return "Error: no entry points"

        refl = self.ctrl.GetShader(pid, sid, entries[0])
        if not refl:
            return "Error: no reflection"

        if not target:
            targets = self.ctrl.GetDisassemblyTargets(True)
            target = targets[0] if targets else ""

        return self.ctrl.DisassembleShader(pid, refl, target)

    # --- texture data ---

    def pick_pixel(self, texture_id: int, x: int, y: int) -> dict:
        rd = self.rd
        tid = rd.ResourceId()
        tid.id = texture_id  # type: ignore
        sub = rd.Subresource(0, 0, 0)
        val = self.ctrl.PickPixel(tid, x, y, sub, rd.CompType.Typeless)
        return {
            "x": x, "y": y,
            "r": val.floatValue[0],
            "g": val.floatValue[1],
            "b": val.floatValue[2],
            "a": val.floatValue[3],
        }

    def get_texture_minmax(self, texture_id: int) -> dict:
        rd = self.rd
        tid = rd.ResourceId()
        tid.id = texture_id  # type: ignore
        sub = rd.Subresource(0, 0, 0)
        mn, mx = self.ctrl.GetMinMax(tid, sub, rd.CompType.Typeless)
        return {
            "min": {"r": mn.floatValue[0], "g": mn.floatValue[1], "b": mn.floatValue[2], "a": mn.floatValue[3]},
            "max": {"r": mx.floatValue[0], "g": mx.floatValue[1], "b": mx.floatValue[2], "a": mx.floatValue[3]},
        }

    def save_texture(self, texture_id: int, output_path: str, mip: int = 0, slice_idx: int = 0) -> str:
        rd = self.rd
        save = rd.TextureSave()
        tid = rd.ResourceId()
        tid.id = texture_id  # type: ignore
        save.resourceId = tid
        save.mip = mip
        save.slice.sliceIndex = slice_idx

        ext = Path(output_path).suffix.lower()
        fmt_map = {
            ".png": rd.FileType.PNG,
            ".jpg": rd.FileType.JPG,
            ".jpeg": rd.FileType.JPG,
            ".bmp": rd.FileType.BMP,
            ".tga": rd.FileType.TGA,
            ".hdr": rd.FileType.HDR,
            ".exr": rd.FileType.EXR,
            ".dds": rd.FileType.DDS,
        }
        save.destType = fmt_map.get(ext, rd.FileType.PNG)

        result = self.ctrl.SaveTexture(save, output_path)
        if result.code == rd.ResultCode.Succeeded:
            return output_path
        raise RuntimeError(f"SaveTexture failed: {result}")

    # --- buffer data ---

    def get_buffer_data(self, buffer_id: int, offset: int = 0, length: int = 256) -> dict:
        rd = self.rd
        bid = rd.ResourceId()
        bid.id = buffer_id  # type: ignore
        data = self.ctrl.GetBufferData(bid, offset, length)
        return {
            "resourceId": buffer_id,
            "offset": offset,
            "length": len(data),
            "hex": data.hex(),
            "base64": base64.b64encode(data).decode("ascii"),
        }

    # --- pixel history ---

    def pixel_history(self, texture_id: int, x: int, y: int) -> list[dict]:
        rd = self.rd
        tid = rd.ResourceId()
        tid.id = texture_id  # type: ignore
        sub = rd.Subresource(0, 0, 0)
        mods = self.ctrl.PixelHistory(tid, x, y, sub, rd.CompType.Typeless)
        result = []
        for m in mods:
            entry: dict[str, Any] = {
                "eventId": m.eventId,
                "directShaderWrite": m.directShaderWrite,
                "unboundPS": m.unboundPS,
            }
            if m.preMod:
                entry["preMod"] = {
                    "r": m.preMod.col.floatValue[0],
                    "g": m.preMod.col.floatValue[1],
                    "b": m.preMod.col.floatValue[2],
                    "a": m.preMod.col.floatValue[3],
                    "depth": m.preMod.depth,
                    "stencil": m.preMod.stencil,
                }
            if m.postMod:
                entry["postMod"] = {
                    "r": m.postMod.col.floatValue[0],
                    "g": m.postMod.col.floatValue[1],
                    "b": m.postMod.col.floatValue[2],
                    "a": m.postMod.col.floatValue[3],
                    "depth": m.postMod.depth,
                    "stencil": m.postMod.stencil,
                }
            result.append(entry)
        return result

    # --- shader debugging ---

    def debug_pixel(self, x: int, y: int) -> dict:
        rd = self.rd
        inputs = rd.DebugPixelInputs()
        inputs.sample = 0
        inputs.primitive = ~0  # use default primitive
        trace = self.ctrl.DebugPixel(x, y, inputs)
        if not trace or not trace.debugger:
            return {"error": "Debug trace not available for this pixel"}

        states = self.ctrl.ContinueDebug(trace.debugger)
        result_states = []
        while states:
            for s in states:
                step = {"stepIndex": s.stepIndex}
                if s.sourceVars:
                    step["sourceVars"] = [
                        {"name": v.name, "value": str(v.value)}
                        for v in s.sourceVars[:20]  # limit output
                    ]
                result_states.append(step)
            states = self.ctrl.ContinueDebug(trace.debugger)

        self.ctrl.FreeTrace(trace)
        return {"pixelX": x, "pixelY": y, "steps": len(result_states), "trace": result_states[:50]}

    def debug_vertex(self, vertex_id: int, instance_id: int = 0, index: int = 0) -> dict:
        rd = self.rd
        trace = self.ctrl.DebugVertex(vertex_id, instance_id, index, 0)
        if not trace or not trace.debugger:
            return {"error": "Debug trace not available for this vertex"}

        states = self.ctrl.ContinueDebug(trace.debugger)
        result_states = []
        while states:
            for s in states:
                step = {"stepIndex": s.stepIndex}
                if s.sourceVars:
                    step["sourceVars"] = [
                        {"name": v.name, "value": str(v.value)}
                        for v in s.sourceVars[:20]
                    ]
                result_states.append(step)
            states = self.ctrl.ContinueDebug(trace.debugger)

        self.ctrl.FreeTrace(trace)
        return {"vertexId": vertex_id, "steps": len(result_states), "trace": result_states[:50]}

    # --- performance counters ---

    def enumerate_counters(self) -> list[dict]:
        counters = self.ctrl.EnumerateCounters()
        result = []
        for c in counters:
            desc = self.ctrl.DescribeCounter(c)
            result.append({
                "counter": int(c),
                "name": desc.name,
                "description": desc.description,
                "resultType": str(desc.resultType),
                "unit": str(desc.unit),
            })
        return result

    def fetch_counters(self, counter_ids: list[int]) -> list[dict]:
        rd = self.rd
        counters = [rd.GPUCounter(c) for c in counter_ids]
        results = self.ctrl.FetchCounters(counters)
        return [
            {
                "eventId": r.eventId,
                "counter": int(r.counter),
                "value": r.value.d if hasattr(r.value, "d") else r.value.u64,
            }
            for r in results
        ]

    # --- mesh/vertex data ---

    def get_post_vs_data(self, instance: int = 0, view: int = 0) -> dict:
        rd = self.rd
        mesh = self.ctrl.GetPostVSData(instance, view, rd.MeshDataStage.VSOut)
        return {
            "numIndices": mesh.numIndices,
            "topology": str(mesh.topology),
            "indexByteStride": mesh.indexByteStride,
            "vertexByteStride": mesh.vertexByteStride,
            "hasData": mesh.numIndices > 0,
        }

    # --- debug messages ---

    def get_debug_messages(self) -> list[dict]:
        msgs = self.ctrl.GetDebugMessages()
        return [
            {
                "eventId": m.eventId,
                "category": str(m.category),
                "severity": str(m.severity),
                "source": str(m.source),
                "messageId": m.messageID,
                "description": m.description,
            }
            for m in msgs
        ]

    # =================================================================
    # SHADER REVERSE-ENGINEERING & TEXTURE MAPPING
    # =================================================================

    def _make_rid(self, id_int: int):
        """Helper: create a ResourceId from an int."""
        rd = self.rd
        rid = rd.ResourceId()
        rid.id = id_int  # type: ignore
        return rid

    def _get_refl_for_stage(self, stage):
        """Get ShaderReflection for a pipeline stage at the current event."""
        ps = self.ctrl.GetPipelineState()
        shader_id = ps.GetShader(stage)
        if not shader_id or int(shader_id) == 0:
            return None, None, None
        pipeline_id = self._get_pipeline_object(ps)
        entries = self.ctrl.GetShaderEntryPoints(shader_id)
        if not entries:
            return shader_id, pipeline_id, None
        refl = self.ctrl.GetShader(pipeline_id, shader_id, entries[0])
        return shader_id, pipeline_id, refl

    def _get_pipeline_object(self, ps):
        """Get pipeline object ID, compatible with D3D11/D3D12/Vulkan/OpenGL."""
        rd = self.rd
        # Method 1: Universal API
        if hasattr(ps, 'GetShaderPipelineObject'):
            try:
                pid = ps.GetShaderPipelineObject()
                if pid and int(pid) != 0:
                    return pid
            except Exception:
                pass
        # Method 2: Vulkan
        try:
            vk = self.ctrl.GetVulkanPipelineState()
            if vk:
                for attr in ['graphics', 'compute']:
                    sub = getattr(vk, attr, None)
                    if sub and hasattr(sub, 'pipelineResourceId'):
                        pid = sub.pipelineResourceId
                        if pid and int(pid) != 0:
                            return pid
        except Exception:
            pass
        # Method 3: D3D12
        try:
            d3d12 = self.ctrl.GetD3D12PipelineState()
            if d3d12:
                if hasattr(d3d12, 'pipelineResourceId'):
                    pid = d3d12.pipelineResourceId
                    if pid and int(pid) != 0:
                        return pid
                for attr in ['graphics', 'compute']:
                    sub = getattr(d3d12, attr, None)
                    if sub and hasattr(sub, 'pipelineResourceId'):
                        pid = sub.pipelineResourceId
                        if pid and int(pid) != 0:
                            return pid
        except Exception:
            pass
        # D3D11/OpenGL: no pipeline object, return null
        return rd.ResourceId()

    # ---- Bound texture mapping per shader stage ----

    def get_bound_textures(self, stage_name: str = "fragment") -> list[dict]:
        """Get all textures currently bound to a shader stage with slot mapping.

        For each texture slot declared in the shader, resolves the actually
        bound resource ID plus its TextureDescription (name, size, format).

        Args:
            stage_name: One of "vertex", "fragment", "geometry", "compute".
        """
        rd = self.rd
        stage_map = {
            "vertex": rd.ShaderStage.Vertex,
            "fragment": rd.ShaderStage.Fragment,
            "pixel": rd.ShaderStage.Fragment,
            "geometry": rd.ShaderStage.Geometry,
            "compute": rd.ShaderStage.Compute,
            "hull": rd.ShaderStage.Hull,
            "domain": rd.ShaderStage.Domain,
            "tess_ctrl": rd.ShaderStage.Hull,
            "tess_eval": rd.ShaderStage.Domain,
        }
        stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

        shader_id, pipeline_id, refl = self._get_refl_for_stage(stage)
        if not refl:
            return []

        ps = self.ctrl.GetPipelineState()
        # Build a lookup for all textures
        tex_lookup: dict[int, Any] = {}
        for t in self.ctrl.GetTextures():
            tex_lookup[int(t.resourceId)] = t

        results = []
        for i, ro in enumerate(refl.readOnlyResources):
            if not ro.isTexture:
                continue
            # Resolve the actual bound resource via the pipeline state
            bind = ps.GetReadOnlyResources(stage)
            bound_rid = 0
            if bind and i < len(bind):
                for desc in bind[i].resources:
                    rid_int = int(desc.resource)
                    if rid_int != 0:
                        bound_rid = rid_int
                        break

            entry: dict[str, Any] = {
                "slotIndex": i,
                "shaderVarName": ro.name,
                "bindPoint": ro.fixedBindNumber,
                "resourceType": str(ro.resType),
                "boundResourceId": bound_rid,
            }

            # Enrich with texture info
            if bound_rid and bound_rid in tex_lookup:
                t = tex_lookup[bound_rid]
                entry["texture"] = {
                    "name": t.name,
                    "width": t.width,
                    "height": t.height,
                    "depth": t.depth,
                    "format": str(t.format),
                    "mips": t.mips,
                    "arraysize": t.arraysize,
                }

            # Infer texture purpose from name heuristics
            entry["inferredRole"] = _infer_texture_role(ro.name, entry.get("texture", {}))

            results.append(entry)

        return results

    def get_bound_samplers(self, stage_name: str = "fragment") -> list[dict]:
        """Get all samplers bound to a shader stage.

        Args:
            stage_name: One of "vertex", "fragment", "geometry", "compute".
        """
        rd = self.rd
        stage_map = {
            "vertex": rd.ShaderStage.Vertex,
            "fragment": rd.ShaderStage.Fragment,
            "pixel": rd.ShaderStage.Fragment,
            "geometry": rd.ShaderStage.Geometry,
            "compute": rd.ShaderStage.Compute,
        }
        stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

        shader_id, pipeline_id, refl = self._get_refl_for_stage(stage)
        if not refl:
            return []

        ps = self.ctrl.GetPipelineState()
        samplers_bound = ps.GetSamplers(stage)

        results = []
        for i, s in enumerate(refl.samplers):
            entry: dict[str, Any] = {
                "slotIndex": i,
                "name": s.name,
                "bindPoint": s.fixedBindNumber,
            }
            # Try to get actual sampler state
            if samplers_bound and i < len(samplers_bound):
                smp = samplers_bound[i]
                entry["addressU"] = str(smp.addressU) if hasattr(smp, "addressU") else None
                entry["addressV"] = str(smp.addressV) if hasattr(smp, "addressV") else None
                entry["filter"] = str(smp.filter) if hasattr(smp, "filter") else None
            results.append(entry)

        return results

    # ---- CBuffer runtime values ----

    def get_cbuffer_contents(self, stage_name: str, cbuf_index: int = 0) -> dict:
        """Read the actual runtime values of a constant buffer at the current event.

        Args:
            stage_name: Shader stage ("vertex", "fragment", etc.).
            cbuf_index: Index of the constant buffer in the shader reflection.
        """
        rd = self.rd
        stage_map = {
            "vertex": rd.ShaderStage.Vertex,
            "fragment": rd.ShaderStage.Fragment,
            "pixel": rd.ShaderStage.Fragment,
            "geometry": rd.ShaderStage.Geometry,
            "compute": rd.ShaderStage.Compute,
        }
        stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

        shader_id, pipeline_id, refl = self._get_refl_for_stage(stage)
        if not refl:
            return {"error": "No shader bound at this stage"}

        if cbuf_index >= len(refl.constantBlocks):
            return {"error": f"cbuf_index {cbuf_index} out of range (max {len(refl.constantBlocks) - 1})"}

        cb = refl.constantBlocks[cbuf_index]

        # Resolve the actual buffer resource
        ps = self.ctrl.GetPipelineState()
        cbufs_bound = ps.GetConstantBuffers(stage, False)
        bound_buf = 0
        buf_offset = 0
        buf_size = 0
        if cbufs_bound and cbuf_index < len(cbufs_bound):
            b = cbufs_bound[cbuf_index]
            bound_buf = int(b.resource)
            buf_offset = b.byteOffset
            buf_size = b.byteSize

        # Read the variable values via the API
        try:
            variables = self.ctrl.GetCBufferVariableContents(
                pipeline_id, shader_id, stage,
                refl.entryPoint if hasattr(refl, "entryPoint") else "",
                cbuf_index,
                self._make_rid(bound_buf),
                buf_offset,
                buf_size,
            )
        except Exception:
            variables = []

        def _var_to_dict(v) -> dict:
            d: dict[str, Any] = {
                "name": v.name,
                "type": str(v.type) if hasattr(v, "type") else "unknown",
                "rows": v.rows if hasattr(v, "rows") else 0,
                "columns": v.columns if hasattr(v, "columns") else 0,
            }
            # Extract float/int values
            if hasattr(v, "value"):
                val = v.value
                if hasattr(val, "f32v"):
                    d["floatValues"] = list(val.f32v[:16])
                if hasattr(val, "u32v"):
                    d["uintValues"] = list(val.u32v[:16])
                if hasattr(val, "s32v"):
                    d["intValues"] = list(val.s32v[:16])
            # Recurse into members
            if hasattr(v, "members") and v.members:
                d["members"] = [_var_to_dict(m) for m in v.members]
            return d

        return {
            "cbufferName": cb.name,
            "cbufferIndex": cbuf_index,
            "byteSize": cb.byteSize,
            "boundBufferId": bound_buf,
            "bufferOffset": buf_offset,
            "variables": [_var_to_dict(v) for v in variables],
        }

    # ---- Full shader reverse-engineering ----

    def reverse_shader(self, stage_name: str = "fragment") -> dict:
        """Reverse-engineer a shader: get disassembly, all available high-level
        source representations, reflection data, bound textures, samplers,
        and constant buffer values.

        This is the "one-stop" tool for understanding what a shader does.

        Args:
            stage_name: One of "vertex", "fragment"/"pixel", "geometry", "compute".
        """
        rd = self.rd
        stage_map = {
            "vertex": rd.ShaderStage.Vertex,
            "fragment": rd.ShaderStage.Fragment,
            "pixel": rd.ShaderStage.Fragment,
            "geometry": rd.ShaderStage.Geometry,
            "compute": rd.ShaderStage.Compute,
            "hull": rd.ShaderStage.Hull,
            "domain": rd.ShaderStage.Domain,
        }
        stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

        shader_id, pipeline_id, refl = self._get_refl_for_stage(stage)
        if not refl:
            return {"error": f"No shader bound at stage '{stage_name}'"}

        result: dict[str, Any] = {
            "stage": stage_name,
            "shaderId": int(shader_id),
            "pipelineId": int(pipeline_id),
            "entryPoint": refl.entryPoint if hasattr(refl, "entryPoint") else "",
        }

        # 1) Get all disassembly targets and disassemble in each
        targets = self.ctrl.GetDisassemblyTargets(True)
        result["disassemblyTargets"] = list(targets)

        disassemblies = {}
        for t in targets:
            try:
                code = self.ctrl.DisassembleShader(pipeline_id, refl, t)
                disassemblies[t] = code
            except Exception as e:
                disassemblies[t] = f"Error: {e}"
        result["disassembly"] = disassemblies

        # 2) Debug info — embedded source if available
        if refl.debugInfo:
            di = refl.debugInfo
            result["debugInfo"] = {
                "compileFlags": str(di.compileFlags) if di.compileFlags else None,
                "encoding": str(di.encoding) if hasattr(di, "encoding") else None,
            }
            # Source files embedded in the shader
            if hasattr(di, "files") and di.files:
                result["sourceFiles"] = [
                    {"filename": f.filename, "contents": f.contents}
                    for f in di.files
                ]
            # Editable source
            if hasattr(di, "editBaseFile") and di.editBaseFile:
                result["editableSource"] = di.editBaseFile

        # 3) Input / output signatures
        result["inputSignature"] = [
            {
                "varName": sig.varName,
                "semanticName": sig.semanticName,
                "semanticIndex": sig.semanticIndex,
                "compType": str(sig.compType),
                "compCount": sig.compCount,
                "regIndex": sig.regIndex,
            }
            for sig in refl.inputSignature
        ]
        result["outputSignature"] = [
            {
                "varName": sig.varName,
                "semanticName": sig.semanticName,
                "semanticIndex": sig.semanticIndex,
                "compType": str(sig.compType),
                "compCount": sig.compCount,
                "regIndex": sig.regIndex,
            }
            for sig in refl.outputSignature
        ]

        # 4) Constant buffers with LIVE values
        result["constantBuffers"] = []
        for idx, cb in enumerate(refl.constantBlocks):
            try:
                cb_data = self.get_cbuffer_contents(stage_name, idx)
            except Exception:
                cb_data = {"error": "Failed to read cbuffer"}
            result["constantBuffers"].append(cb_data)

        # 5) Bound textures with auto-matching
        result["boundTextures"] = self.get_bound_textures(stage_name)

        # 6) Bound samplers
        result["boundSamplers"] = self.get_bound_samplers(stage_name)

        # 7) Read/write resources
        result["readWriteResources"] = [
            {
                "name": r.name,
                "bindPoint": r.fixedBindNumber,
                "type": str(r.resType),
                "isTexture": r.isTexture,
            }
            for r in refl.readWriteResources
        ]

        return result

    def get_all_disassembly_targets(self) -> list[str]:
        """List available disassembly target languages (e.g. HLSL, GLSL, SPIR-V)."""
        return list(self.ctrl.GetDisassemblyTargets(True))

    def get_shader_source(self, stage_name: str = "fragment") -> dict:
        """Extract embedded source code from a shader (if available).

        Many shaders compiled in debug mode contain the original HLSL/GLSL source.

        Args:
            stage_name: Shader stage name.
        """
        rd = self.rd
        stage_map = {
            "vertex": rd.ShaderStage.Vertex,
            "fragment": rd.ShaderStage.Fragment,
            "pixel": rd.ShaderStage.Fragment,
            "geometry": rd.ShaderStage.Geometry,
            "compute": rd.ShaderStage.Compute,
        }
        stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

        shader_id, pipeline_id, refl = self._get_refl_for_stage(stage)
        if not refl:
            return {"error": f"No shader at stage '{stage_name}'"}

        result: dict[str, Any] = {
            "stage": stage_name,
            "shaderId": int(shader_id),
            "hasDebugInfo": refl.debugInfo is not None,
        }

        if refl.debugInfo:
            di = refl.debugInfo
            if hasattr(di, "files") and di.files:
                result["sourceFiles"] = [
                    {"filename": f.filename, "contents": f.contents}
                    for f in di.files
                ]
            else:
                result["sourceFiles"] = []

            # Try each disassembly target to find HLSL/GLSL
            targets = self.ctrl.GetDisassemblyTargets(True)
            hlsl_targets = [t for t in targets if "hlsl" in t.lower() or "glsl" in t.lower()]
            if hlsl_targets:
                result["highLevelDisassembly"] = {}
                for t in hlsl_targets:
                    try:
                        result["highLevelDisassembly"][t] = self.ctrl.DisassembleShader(
                            pipeline_id, refl, t
                        )
                    except Exception as e:
                        result["highLevelDisassembly"][t] = f"Error: {e}"
        else:
            result["note"] = "No debug info embedded. Use disassemble_shader for IL/bytecode."
            # Still try high-level targets
            targets = self.ctrl.GetDisassemblyTargets(True)
            hlsl_targets = [t for t in targets if "hlsl" in t.lower() or "glsl" in t.lower()]
            if hlsl_targets:
                result["highLevelDisassembly"] = {}
                for t in hlsl_targets:
                    try:
                        result["highLevelDisassembly"][t] = self.ctrl.DisassembleShader(
                            pipeline_id, refl, t
                        )
                    except Exception as e:
                        result["highLevelDisassembly"][t] = f"Error: {e}"

        return result

    # ---- Export drawcall: shader + textures in one shot ----

    def export_drawcall(self, event_id: int, output_dir: str) -> dict:
        """One-shot export of everything about a draw call:
        - Vertex & Fragment shader disassembly + source (if available)
        - All bound textures saved as PNG with slot-name filenames
        - Constant buffer values
        - Pipeline state summary

        Args:
            event_id: The event ID of the draw call.
            output_dir: Directory to save exported files to.
        """
        os.makedirs(output_dir, exist_ok=True)
        self.set_event(event_id)

        result: dict[str, Any] = {
            "eventId": event_id,
            "outputDir": output_dir,
            "exports": [],
        }

        # Pipeline state
        ps_data = self.get_pipeline_state()
        result["pipelineState"] = ps_data

        stages_to_export = ["vertex", "fragment"]
        for stage_name in stages_to_export:
            stage_result: dict[str, Any] = {"stage": stage_name}

            try:
                # Get shader source / disassembly
                source = self.get_shader_source(stage_name)
                stage_result["shaderInfo"] = source

                # Save disassembly to file
                targets = self.get_all_disassembly_targets()
                for t in targets:
                    safe_name = t.replace(" ", "_").replace("/", "_")
                    filename = f"{stage_name}_{safe_name}.txt"
                    filepath = os.path.join(output_dir, filename)
                    try:
                        rd = self.rd
                        stage_map = {
                            "vertex": rd.ShaderStage.Vertex,
                            "fragment": rd.ShaderStage.Fragment,
                        }
                        stage = stage_map.get(stage_name, rd.ShaderStage.Fragment)
                        sid, pid, refl = self._get_refl_for_stage(stage)
                        if refl:
                            code = self.ctrl.DisassembleShader(pid, refl, t)
                            with open(filepath, "w", encoding="utf-8") as f:
                                f.write(code)
                            stage_result.setdefault("savedFiles", []).append(filepath)
                    except Exception:
                        pass

                # Save embedded source
                if "sourceFiles" in source:
                    for sf in source["sourceFiles"]:
                        safe_fn = sf["filename"].replace("/", "_").replace("\\", "_")
                        filepath = os.path.join(output_dir, f"{stage_name}_{safe_fn}")
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(sf["contents"])
                        stage_result.setdefault("savedFiles", []).append(filepath)

                # Export bound textures as PNG
                bound = self.get_bound_textures(stage_name)
                stage_result["boundTextures"] = []
                for tex_info in bound:
                    tex_entry = {
                        "slot": tex_info["slotIndex"],
                        "name": tex_info["shaderVarName"],
                        "role": tex_info["inferredRole"],
                        "resourceId": tex_info["boundResourceId"],
                    }
                    if tex_info["boundResourceId"]:
                        safe_var = tex_info["shaderVarName"].replace(" ", "_")
                        role = tex_info["inferredRole"]
                        tex_filename = f"{stage_name}_slot{tex_info['slotIndex']}_{safe_var}_{role}.png"
                        tex_filepath = os.path.join(output_dir, tex_filename)
                        try:
                            self.save_texture(tex_info["boundResourceId"], tex_filepath)
                            tex_entry["savedTo"] = tex_filepath
                        except Exception as e:
                            tex_entry["saveError"] = str(e)
                    stage_result["boundTextures"].append(tex_entry)

                # Constant buffer values
                try:
                    rd = self.rd
                    stage_map2 = {
                        "vertex": rd.ShaderStage.Vertex,
                        "fragment": rd.ShaderStage.Fragment,
                    }
                    s = stage_map2.get(stage_name, rd.ShaderStage.Fragment)
                    _, _, refl2 = self._get_refl_for_stage(s)
                    if refl2:
                        cbufs = []
                        for idx in range(len(refl2.constantBlocks)):
                            try:
                                cbufs.append(self.get_cbuffer_contents(stage_name, idx))
                            except Exception:
                                pass
                        stage_result["constantBuffers"] = cbufs
                except Exception:
                    pass

            except Exception as e:
                stage_result["error"] = str(e)

            result["exports"].append(stage_result)

        return result


# ---------------------------------------------------------------------------
# Heuristic: infer texture role from shader variable name and texture props
# ---------------------------------------------------------------------------

def _infer_texture_role(var_name: str, tex_info: dict) -> str:
    """Guess what role a texture serves based on naming conventions.

    Returns one of: albedo, normal, metallic, roughness, ao, emissive,
    specular, height, opacity, shadow, environment, lightmap, unknown.
    """
    name = var_name.lower()
    tex_name = tex_info.get("name", "").lower() if tex_info else ""
    combined = name + " " + tex_name

    # Common naming patterns used in game engines
    patterns = [
        # (keywords, role)
        (["albedo", "diffuse", "basecolor", "base_color", "color", "_col", "maintex"], "albedo"),
        (["normal", "nrm", "bump", "normalmap", "_n_", "_norm"], "normal"),
        (["metallic", "metalness", "_met", "metal"], "metallic"),
        (["rough", "roughness", "_rgh", "smoothness"], "roughness"),
        (["ao", "ambient_occlusion", "occlusion", "_occ"], "ambient_occlusion"),
        (["emissive", "emission", "glow", "self_illum"], "emissive"),
        (["specular", "spec", "_spc"], "specular"),
        (["height", "displacement", "parallax", "_hgt"], "height"),
        (["opacity", "alpha", "transparent", "mask"], "opacity"),
        (["shadow", "shadowmap", "shadow_map"], "shadow_map"),
        (["env", "environment", "cubemap", "skybox", "reflection", "ibl"], "environment"),
        (["light", "lightmap", "lm_"], "lightmap"),
        (["detail", "detail_albedo", "detail_normal"], "detail"),
        (["noise", "dither"], "noise"),
        (["lut", "lookup", "gradient", "ramp"], "lookup_table"),
        (["depth", "zbuffer"], "depth"),
        (["stencil"], "stencil"),
        (["ssao"], "ssao"),
        (["ssr", "screen_space_reflection"], "ssr"),
        (["bloom", "glow_map"], "bloom"),
    ]

    for keywords, role in patterns:
        for kw in keywords:
            if kw in combined:
                return role

    # Check texture format for clues
    fmt = tex_info.get("format", "").lower() if tex_info else ""
    if "bc5" in fmt or "rg" in fmt.split("_")[0]:
        return "normal"  # BC5/RG formats are typically normals
    if "bc6" in fmt or "hdr" in fmt:
        return "environment"  # HDR formats are typically environment maps

    return "unknown"


# ---------------------------------------------------------------------------
# Session Manager — singleton that keeps track of loaded captures
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages multiple loaded capture sessions."""

    def __init__(self):
        self._sessions: dict[str, CaptureSession] = {}
        self._active: Optional[str] = None

    @property
    def active(self) -> CaptureSession:
        if self._active is None or self._active not in self._sessions:
            raise RuntimeError("No capture is loaded. Use 'load_capture' first.")
        return self._sessions[self._active]

    def load(self, path: str) -> str:
        abs_path = os.path.abspath(path)
        if abs_path in self._sessions:
            self._active = abs_path
            return f"Capture already loaded: {abs_path}"
        session = CaptureSession(abs_path)
        self._sessions[abs_path] = session
        self._active = abs_path
        return f"Loaded capture: {abs_path}"

    def close(self, path: Optional[str] = None) -> str:
        target = os.path.abspath(path) if path else self._active
        if not target or target not in self._sessions:
            return "No such capture loaded"
        self._sessions[target].close()
        del self._sessions[target]
        if self._active == target:
            self._active = next(iter(self._sessions), None)
        return f"Closed capture: {target}"

    def list_loaded(self) -> list[str]:
        return list(self._sessions.keys())

    def switch(self, path: str) -> str:
        abs_path = os.path.abspath(path)
        if abs_path not in self._sessions:
            return f"Capture not loaded: {abs_path}"
        self._active = abs_path
        return f"Switched to: {abs_path}"

    def close_all(self):
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()
        self._active = None
