"""
RenderDoc MCP Extension — File IPC bridge for external MCP server.

Architecture:
    [AI / WorkBuddy] <-stdio-> [External MCP Server] <-file IPC-> [This Extension inside RenderDoc]

This extension runs inside RenderDoc's Python 3.6 environment.
It polls for request.json, executes commands via pyrenderdoc API,
and writes response.json.

IPC directory: %TEMP%/renderdoc_mcp_ipc/
"""

import json
import os
import sys
import traceback
import tempfile
import time

import renderdoc as rd

try:
    import qrenderdoc as qrd
    from PySide2 import QtCore
    HAS_UI = True
except ImportError:
    HAS_UI = False

# ---- IPC paths ----
IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp_ipc")
REQ_FILE = os.path.join(IPC_DIR, "request.json")
RESP_FILE = os.path.join(IPC_DIR, "response.json")
READY_FILE = os.path.join(IPC_DIR, "ready")  # Signal: request is ready to read

# ---- Global state ----
_ctx = None
_poller = None


# =====================================================================
# IPC Poller using QObject.startTimer (most reliable in RenderDoc)
# =====================================================================

if HAS_UI:
    class JsonPoller(QtCore.QObject):
        """Polls for IPC request files using QObject built-in timer."""

        def __init__(self, handler_fn, parent=None):
            super(JsonPoller, self).__init__(parent)
            self._handler = handler_fn
            self._timer_id = 0

        def start(self):
            if not os.path.exists(IPC_DIR):
                os.makedirs(IPC_DIR)
            # Clean stale files
            for f in [REQ_FILE, RESP_FILE, READY_FILE]:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            # startTimer returns a timer ID, fires timerEvent every 200ms
            self._timer_id = self.startTimer(200)
            print("[RD-MCP] Poller started (timer_id=%d, dir=%s)" % (self._timer_id, IPC_DIR))

        def stop(self):
            if self._timer_id:
                self.killTimer(self._timer_id)
                self._timer_id = 0
            print("[RD-MCP] Poller stopped")

        def timerEvent(self, event):
            """Called by Qt event loop every 200ms."""
            # Only read when the READY signal file exists
            if not os.path.exists(READY_FILE):
                return
            if not os.path.exists(REQ_FILE):
                return
            try:
                # Remove ready signal first
                os.remove(READY_FILE)
                # Now safe to read — client has finished writing
                with open(REQ_FILE, "r") as f:
                    raw = f.read()
                os.remove(REQ_FILE)

                request = json.loads(raw)
                response = self._handler(request)

                with open(RESP_FILE, "w") as f:
                    json.dump(response, f, ensure_ascii=False, default=str)
            except Exception as e:
                print("[RD-MCP] Error: %s" % str(e))
                traceback.print_exc()
                try:
                    if os.path.exists(REQ_FILE):
                        os.remove(REQ_FILE)
                    with open(RESP_FILE, "w") as f:
                        json.dump({"error": {"code": -1, "message": str(e)}}, f)
                except Exception:
                    pass


# =====================================================================
# Request handler — executes commands using pyrenderdoc
# =====================================================================

def handle_request(request):
    """Route a request to the appropriate handler function."""
    rid = request.get("id", "?")
    method = request.get("method", "")
    params = request.get("params", {})

    if method not in METHODS:
        return {"id": rid, "error": {"code": -32601, "message": "Unknown method: %s" % method}}

    try:
        result = METHODS[method](params)
        return {"id": rid, "result": result}
    except Exception as e:
        traceback.print_exc()
        return {"id": rid, "error": {"code": -32000, "message": str(e)}}


# ---- Helper: run on replay thread ----
def _replay(fn):
    """Execute fn(controller) on the replay thread, return result."""
    result = [None]
    error = [None]

    def _invoke(ctrl):
        try:
            result[0] = fn(ctrl)
        except Exception as e:
            error[0] = e

    _ctx.Replay().BlockInvoke(_invoke)
    if error[0]:
        raise error[0]
    return result[0]


# =====================================================================
# Method implementations
# =====================================================================

def _ping(params):
    return {"status": "ok", "server": "renderdoc_mcp", "ipc_dir": IPC_DIR}


def _get_capture_status(params):
    """Check if capture is loaded."""
    if not _ctx.IsCaptureLoaded():
        return {"loaded": False}

    def _do(ctrl):
        props = ctrl.GetAPIProperties()
        fi = ctrl.GetFrameInfo()
        result = {
            "loaded": True,
            "api": str(props.pipelineType),
            "renderer": str(props.localRenderer),
            "frameNumber": fi.frameNumber,
        }
        # Stats structure varies by RenderDoc version
        try:
            result["draws"] = fi.stats.recorded.draws
            result["dispatches"] = fi.stats.recorded.dispatches
            result["calls"] = fi.stats.recorded.calls
        except AttributeError:
            try:
                result["draws"] = fi.stats.draws
                result["dispatches"] = fi.stats.dispatches
                result["calls"] = fi.stats.calls
            except AttributeError:
                result["stats_note"] = "Stats not available in this RenderDoc version"
        return result
    return _replay(_do)


def _get_draw_calls(params):
    """Get all draw calls."""
    def _flatten(actions, depth=0):
        result = []
        for a in actions:
            entry = {
                "eventId": a.eventId,
                "name": a.customName if a.customName else "Action %d" % a.actionId,
                "flags": int(a.flags),
                "numIndices": a.numIndices,
                "numInstances": a.numInstances,
                "depth": depth,
            }
            if a.outputs:
                entry["outputs"] = [int(o) for o in a.outputs if int(o) != 0]
            result.append(entry)
            if a.children:
                result.extend(_flatten(a.children, depth + 1))
        return result

    marker_filter = params.get("marker_filter", "")
    event_min = params.get("event_id_min", 0)
    event_max = params.get("event_id_max", 0)

    def _do(ctrl):
        all_actions = _flatten(ctrl.GetRootActions())
        filtered = all_actions
        if marker_filter:
            filtered = [a for a in filtered if marker_filter.lower() in a["name"].lower()]
        if event_min > 0:
            filtered = [a for a in filtered if a["eventId"] >= event_min]
        if event_max > 0:
            filtered = [a for a in filtered if a["eventId"] <= event_max]
        return {"count": len(filtered), "actions": filtered}

    return _replay(_do)


def _get_pipeline_obj(ps, ctrl):
    """Get pipeline object ID, compatible with D3D11/D3D12/Vulkan/OpenGL."""
    # Method 1: Universal API (newer RenderDoc versions)
    if hasattr(ps, 'GetShaderPipelineObject'):
        try:
            pid = ps.GetShaderPipelineObject()
            if pid and int(pid) != 0:
                return pid
        except Exception:
            pass

    # Method 2: Vulkan — pipeline from VkGraphicsPipelineState
    try:
        vk = ctrl.GetVulkanPipelineState()
        if vk and hasattr(vk, 'graphics') and hasattr(vk.graphics, 'pipelineResourceId'):
            pid = vk.graphics.pipelineResourceId
            if pid and int(pid) != 0:
                return pid
        if vk and hasattr(vk, 'compute') and hasattr(vk.compute, 'pipelineResourceId'):
            pid = vk.compute.pipelineResourceId
            if pid and int(pid) != 0:
                return pid
    except Exception:
        pass

    # Method 3: D3D12 — pipeline from D3D12PipelineState
    try:
        d3d12 = ctrl.GetD3D12PipelineState()
        if d3d12 and hasattr(d3d12, 'pipelineResourceId'):
            pid = d3d12.pipelineResourceId
            if pid and int(pid) != 0:
                return pid
        # D3D12 graphics/compute pipeline
        if d3d12:
            for attr in ['graphics', 'compute']:
                sub = getattr(d3d12, attr, None)
                if sub and hasattr(sub, 'pipelineResourceId'):
                    pid = sub.pipelineResourceId
                    if pid and int(pid) != 0:
                        return pid
    except Exception:
        pass

    # Method 4: D3D11 — no explicit pipeline object, use null ResourceId
    # D3D11 doesn't have pipeline state objects; RenderDoc handles this internally
    # GetShader/GetShaderEntryPoints still work with a null pipeline ID
    try:
        d3d11 = ctrl.GetD3D11PipelineState()
        if d3d11:
            # D3D11 is valid but has no pipeline object — return null
            return rd.ResourceId()
    except Exception:
        pass

    # Method 5: OpenGL — similar to D3D11, no explicit pipeline object
    try:
        gl = ctrl.GetOpenGLPipelineState()
        if gl:
            return rd.ResourceId()
    except Exception:
        pass

    # Fallback: return a null ResourceId (works for D3D11 and some edge cases)
    return rd.ResourceId()


def _get_pipeline_state(params):
    """Get pipeline state at an event."""
    event_id = params.get("event_id")
    if event_id is None:
        raise ValueError("event_id required")

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()
        pipeline_id = _get_pipeline_obj(ps, ctrl)

        stages = {}
        for name, stage in [("vertex", rd.ShaderStage.Vertex),
                            ("hull", rd.ShaderStage.Hull),
                            ("domain", rd.ShaderStage.Domain),
                            ("geometry", rd.ShaderStage.Geometry),
                            ("fragment", rd.ShaderStage.Fragment),
                            ("compute", rd.ShaderStage.Compute)]:
            sid = ps.GetShader(stage)
            if sid and int(sid) != 0:
                stages[name] = {"shaderId": int(sid)}
                entries = ctrl.GetShaderEntryPoints(sid)
                if entries:
                    refl = ctrl.GetShader(pipeline_id, sid, entries[0])
                    if refl:
                        stages[name]["entryPoint"] = entries[0].name
                        stages[name]["textureCount"] = sum(1 for r in refl.readOnlyResources if r.isTexture)
                        stages[name]["cbufferCount"] = len(refl.constantBlocks)
                        stages[name]["samplerCount"] = len(refl.samplers)

        targets = ps.GetOutputTargets()
        depth = ps.GetDepthTarget()

        return {
            "shaders": stages,
            "renderTargets": [int(t.resource) for t in targets if int(t.resource) != 0],
            "depthTarget": int(depth.resource) if depth and int(depth.resource) != 0 else None,
        }

    return _replay(_do)


def _get_shader_info(params):
    """Get detailed shader info + disassembly + bound textures at an event."""
    event_id = params.get("event_id")
    stage_name = params.get("stage", "fragment")
    if event_id is None:
        raise ValueError("event_id required")

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

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()
        sid = ps.GetShader(stage)
        if not sid or int(sid) == 0:
            return {"error": "No shader at stage '%s'" % stage_name}

        pid = _get_pipeline_obj(ps, ctrl)
        entries = ctrl.GetShaderEntryPoints(sid)
        if not entries:
            return {"error": "No entry points"}

        refl = ctrl.GetShader(pid, sid, entries[0])
        if not refl:
            return {"error": "No reflection"}

        result = {
            "shaderId": int(sid),
            "pipelineId": int(pid),
            "stage": stage_name,
            "entryPoint": entries[0].name,
        }

        # Disassembly — all targets
        targets = ctrl.GetDisassemblyTargets(True)
        result["disassemblyTargets"] = list(targets)
        disasm = {}
        for t in targets:
            try:
                disasm[t] = ctrl.DisassembleShader(pid, refl, t)
            except Exception as e:
                disasm[t] = "Error: %s" % str(e)
        result["disassembly"] = disasm

        # Embedded source
        if refl.debugInfo and hasattr(refl.debugInfo, "files") and refl.debugInfo.files:
            result["sourceFiles"] = [
                {"filename": f.filename, "contents": f.contents}
                for f in refl.debugInfo.files
            ]

        # Input/output — safe attribute access for different RenderDoc versions
        def _sig(s):
            return {
                "name": getattr(s, 'varName', getattr(s, 'semanticName', '')),
                "semantic": getattr(s, 'semanticName', ''),
                "idx": getattr(s, 'semanticIndex', 0),
                "type": str(getattr(s, 'compType', getattr(s, 'varType', ''))),
                "count": getattr(s, 'compCount', 0),
            }

        result["inputs"] = [_sig(s) for s in refl.inputSignature]
        result["outputs"] = [_sig(s) for s in refl.outputSignature]

        # Bound textures
        name_lookup = {}
        for r in ctrl.GetResources():
            name_lookup[int(r.resourceId)] = r.name

        tex_lookup = {}
        for t in ctrl.GetTextures():
            tex_lookup[int(t.resourceId)] = t

        bound_textures = []
        try:
            ro_bindings = ps.GetReadOnlyResources(stage)
        except Exception:
            ro_bindings = None

        for i, ro in enumerate(getattr(refl, 'readOnlyResources', [])):
            if not getattr(ro, 'isTexture', False):
                continue
            ro_name = getattr(ro, 'name', 'tex%d' % i)
            tex_entry = {
                "slot": i, "name": ro_name, "bind": getattr(ro, 'fixedBindNumber', i),
                "resourceId": 0,
            }
            # Resolve bound resource — compatible with D3D11/D3D12/Vulkan/GL
            tex_entry["resourceId"] = _resolve_bound_resource(ro_bindings, i, ctrl)

            rid = tex_entry["resourceId"]
            if rid and rid in tex_lookup:
                t = tex_lookup[rid]
                tex_entry["texName"] = name_lookup.get(rid, "")
                tex_entry["width"] = t.width
                tex_entry["height"] = t.height
                tex_entry["format"] = _fmt_str(t.format)
                tex_entry["role"] = _infer_role(ro_name, name_lookup.get(rid, ""), _fmt_str(t.format))
            else:
                tex_entry["role"] = _infer_role(ro_name, "", "")
            bound_textures.append(tex_entry)
        result["boundTextures"] = bound_textures

        # Constant buffers with values
        cbufs = []
        try:
            cbufs_bound = ps.GetConstantBuffers(stage, False)
        except Exception:
            cbufs_bound = None
        cb_list = getattr(refl, 'constantBlocks', [])
        for idx, cb in enumerate(cb_list):
            cb_info = {"name": getattr(cb, 'name', ''), "size": getattr(cb, 'byteSize', 0), "index": idx}
            if cbufs_bound and idx < len(cbufs_bound):
                b = cbufs_bound[idx]
                cb_info["bufferId"] = int(b.resource)
                try:
                    vars = ctrl.GetCBufferVariableContents(
                        pid, sid, stage, entries[0].name,
                        idx, b.resource, getattr(b, 'byteOffset', 0), getattr(b, 'byteSize', 0))
                    cb_info["variables"] = [_var_dict(v) for v in vars]
                except Exception:
                    cb_info["variables"] = []
            else:
                try:
                    null_rid = rd.ResourceId()
                    vrs = ctrl.GetCBufferVariableContents(pid, sid, stage, entries[0].name, idx, null_rid, 0, 0)
                    cb_info["variables"] = [_var_dict(v) for v in vrs]
                except Exception:
                    cb_info["variables"] = []
            cbufs.append(cb_info)
        result["constantBuffers"] = cbufs

        # Samplers
        samplers = []
        try:
            smp_bound = ps.GetSamplers(stage)
        except Exception:
            smp_bound = None
        for i, s in enumerate(getattr(refl, 'samplers', [])):
            se = {"slot": i, "name": getattr(s, 'name', '')}
            if smp_bound and i < len(smp_bound):
                sb = smp_bound[i]
                for attr in ['addressU', 'addressV', 'addressW', 'filter']:
                    if hasattr(sb, attr):
                        se[attr] = str(getattr(sb, attr))
            samplers.append(se)
        result["samplers"] = samplers

        return result

    return _replay(_do)


def _fmt_str(fmt):
    """Safely convert ResourceFormat to string."""
    if hasattr(fmt, 'name'):
        return fmt.name
    return str(fmt)


def _get_textures(params):
    """List all textures."""
    def _do(ctrl):
        # Build name lookup from resources
        name_lookup = {}
        for r in ctrl.GetResources():
            name_lookup[int(r.resourceId)] = r.name

        result = []
        for t in ctrl.GetTextures():
            rid = int(t.resourceId)
            entry = {
                "id": rid,
                "name": name_lookup.get(rid, ""),
                "w": t.width, "h": t.height,
                "fmt": _fmt_str(t.format),
                "mips": t.mips,
                "array": t.arraysize,
                "size": t.byteSize,
            }
            result.append(entry)
        return result
    return _replay(_do)


def _get_buffers(params):
    """List all buffers."""
    def _do(ctrl):
        name_lookup = {}
        for r in ctrl.GetResources():
            name_lookup[int(r.resourceId)] = r.name

        return [
            {"id": int(b.resourceId), "name": name_lookup.get(int(b.resourceId), ""), "length": b.length}
            for b in ctrl.GetBuffers()
        ]
    return _replay(_do)


def _get_resources(params):
    """List all resources."""
    def _do(ctrl):
        return [
            {"id": int(r.resourceId), "name": r.name, "type": str(r.type)}
            for r in ctrl.GetResources()
        ]
    return _replay(_do)


def _find_by_texture(params):
    """Find draw calls using a texture name (partial match)."""
    tex_name = params.get("texture_name", "")
    if not tex_name:
        raise ValueError("texture_name required")

    def _do(ctrl):
        # Find matching texture IDs
        matching_ids = set()
        for t in ctrl.GetTextures():
            if tex_name.lower() in t.name.lower():
                matching_ids.add(int(t.resourceId))

        if not matching_ids:
            return {"matches": [], "note": "No textures matching '%s'" % tex_name}

        # Scan actions for usage
        results = []
        def scan(actions):
            for a in actions:
                if a.outputs:
                    for o in a.outputs:
                        if int(o) in matching_ids:
                            results.append({"eventId": a.eventId, "name": a.customName or "Action"})
                # Also check by moving to event and checking bindings (expensive, skip for now)
                if a.children:
                    scan(a.children)
        scan(ctrl.GetRootActions())

        return {
            "textureIds": list(matching_ids),
            "draws": results,
            "note": "Found %d textures, %d draw calls" % (len(matching_ids), len(results)),
        }

    return _replay(_do)


def _save_texture(params):
    """Save a texture to disk. Auto-detects CubeMap (arraysize=6) and handles appropriately.

    For CubeMap textures:
    - DDS/HDR/EXR: saves all 6 faces in a single file (native cubemap support)
    - PNG/JPG/BMP/TGA: saves 6 individual face files ({name}_face0_posX.png, etc.)
      OR a single cross-layout image if slice=-1

    For regular 2D textures: saves normally.

    Args:
        resource_id: Texture resource ID
        output_path: Output file path
        mip: Mip level (default 0)
        slice: For CubeMap: specific face (0-5), -1 for all faces. Ignored for 2D.
    """
    resource_id = params.get("resource_id")
    output_path = params.get("output_path")
    if not resource_id or not output_path:
        raise ValueError("resource_id and output_path required")

    req_slice = params.get("slice", -1)  # -1 = auto (all faces for cubemap)

    def _do(ctrl):
        # Find the actual texture info
        target_tex = None
        for t in ctrl.GetTextures():
            if int(t.resourceId) == int(resource_id):
                target_tex = t
                break

        if target_tex is None:
            return {"error": "Texture %d not found" % int(resource_id)}

        target_rid = target_tex.resourceId
        is_cubemap = target_tex.arraysize == 6
        mip_level = params.get("mip", 0)

        ext = os.path.splitext(output_path)[1].lower()
        fmt_map = {
            ".png": rd.FileType.PNG, ".jpg": rd.FileType.JPG,
            ".bmp": rd.FileType.BMP, ".tga": rd.FileType.TGA,
            ".hdr": rd.FileType.HDR, ".exr": rd.FileType.EXR,
            ".dds": rd.FileType.DDS,
        }
        dest_type = fmt_map.get(ext, rd.FileType.PNG)

        # Non-CubeMap: save normally
        if not is_cubemap:
            save = rd.TextureSave()
            save.resourceId = target_rid
            save.mip = mip_level
            save.destType = dest_type
            ret = ctrl.SaveTexture(save, output_path)
            if ret.code == rd.ResultCode.Succeeded:
                return {"saved": output_path, "type": "2D"}
            return {"error": "Save failed: %s" % str(ret)}

        # CubeMap handling
        face_names = ["posX", "negX", "posY", "negY", "posZ", "negZ"]
        face_labels = ["+X (Right)", "-X (Left)", "+Y (Up)", "-Y (Down)", "+Z (Front)", "-Z (Back)"]

        # DDS can store full cubemap natively
        if dest_type == rd.FileType.DDS:
            save = rd.TextureSave()
            save.resourceId = target_rid
            save.mip = mip_level
            save.destType = rd.FileType.DDS
            ret = ctrl.SaveTexture(save, output_path)
            if ret.code == rd.ResultCode.Succeeded:
                return {
                    "saved": output_path,
                    "type": "cubemap",
                    "format": "DDS (all 6 faces)",
                    "faces": 6,
                    "size": "%dx%d" % (target_tex.width, target_tex.height),
                }
            return {"error": "Save failed: %s" % str(ret)}

        # Specific face requested
        if req_slice >= 0 and req_slice < 6:
            save = rd.TextureSave()
            save.resourceId = target_rid
            save.mip = mip_level
            save.destType = dest_type
            # Set slice for the specific face
            if hasattr(save, 'slice'):
                if hasattr(save.slice, 'sliceIndex'):
                    save.slice.sliceIndex = req_slice
                elif hasattr(save.slice, 'cubeCruciform'):
                    pass  # Will handle differently
            # Try setting via sample attribute
            if hasattr(save, 'sample'):
                pass
            # Direct slice setting for Vulkan/newer RenderDoc
            if hasattr(save, 'resourceId'):
                save.slice = rd.Subresource(mip_level, req_slice, 0)

            ret = ctrl.SaveTexture(save, output_path)
            if ret.code == rd.ResultCode.Succeeded:
                return {
                    "saved": output_path,
                    "type": "cubemap_face",
                    "face": req_slice,
                    "faceName": face_names[req_slice],
                    "faceLabel": face_labels[req_slice],
                }
            return {"error": "Save face %d failed: %s" % (req_slice, str(ret))}

        # Default: export all 6 faces as individual files
        base, base_ext = os.path.splitext(output_path)
        saved_faces = []
        errors = []

        for face_idx in range(6):
            face_path = "%s_face%d_%s%s" % (base, face_idx, face_names[face_idx], base_ext)
            save = rd.TextureSave()
            save.resourceId = target_rid
            save.mip = mip_level
            save.destType = dest_type
            # Set the slice/face
            try:
                save.slice = rd.Subresource(mip_level, face_idx, 0)
            except Exception:
                # Fallback: try setting sliceIndex directly
                if hasattr(save, 'slice') and hasattr(save.slice, 'sliceIndex'):
                    save.slice.sliceIndex = face_idx

            ret = ctrl.SaveTexture(save, face_path)
            if hasattr(ret, 'code') and ret.code == rd.ResultCode.Succeeded:
                saved_faces.append({
                    "path": face_path,
                    "face": face_idx,
                    "name": face_names[face_idx],
                    "label": face_labels[face_idx],
                })
            else:
                errors.append("Face %d (%s): %s" % (face_idx, face_names[face_idx], str(ret)[:80]))

        result = {
            "type": "cubemap",
            "faces": len(saved_faces),
            "size": "%dx%d" % (target_tex.width, target_tex.height),
            "savedFaces": saved_faces,
        }
        if errors:
            result["errors"] = errors
        if saved_faces:
            result["saved"] = saved_faces[0]["path"]  # First face as primary
        else:
            result["error"] = "No faces saved successfully"

        return result

    return _replay(_do)


# ---- Helpers ----

def _var_dict(v):
    d = {"name": v.name}
    if hasattr(v, "value"):
        val = v.value
        if hasattr(val, "f32v"):
            floats = list(val.f32v[:16])
            # Trim trailing zeros for readability
            while floats and floats[-1] == 0.0:
                floats.pop()
            if floats:
                d["float"] = floats
        if hasattr(val, "u32v"):
            uints = list(val.u32v[:4])
            if any(u != 0 for u in uints):
                d["uint"] = uints
        if hasattr(val, "s32v"):
            sints = list(val.s32v[:4])
            if any(s != 0 for s in sints):
                d["int"] = sints
    # Try direct float/int attributes (some RenderDoc versions)
    for attr in ['fvalue', 'uvalue', 'ivalue']:
        val = getattr(v, attr, None)
        if val is not None:
            try:
                d[attr] = list(val[:4])
            except Exception:
                pass
    # Include type and byte offset if available
    if hasattr(v, 'type'):
        try:
            d["type"] = str(v.type)
        except Exception:
            pass
    if hasattr(v, 'byteOffset'):
        d["offset"] = v.byteOffset
    if hasattr(v, 'rows') and hasattr(v, 'columns'):
        d["rows"] = v.rows
        d["columns"] = v.columns
    if hasattr(v, "members") and v.members:
        d["members"] = [_var_dict(m) for m in v.members]
    return d


def _resolve_bound_resource(bindings, index, ctrl):
    """Resolve the actual bound resource ID from pipeline bindings.

    Handles all API-specific binding structures:
    - Vulkan: UsedDescriptor → .descriptor.resource (ResourceId)
    - D3D11: BoundResourceArray → .resources[] → .resourceId
    - D3D12: Similar to D3D11 but may use descriptor tables
    - OpenGL: BoundResource → .resourceId

    Args:
        bindings: Result of ps.GetReadOnlyResources(stage)
        index: The slot index to look up
        ctrl: The ReplayController (for fallback descriptor access)
    Returns:
        int: The bound resource ID, or 0 if not found
    """
    if not bindings or index >= len(bindings):
        return 0

    binding = bindings[index]

    try:
        # --- Strategy 1: Vulkan UsedDescriptor → descriptor.resource ---
        # This is the primary path for Vulkan/ANGLE captures
        if hasattr(binding, 'descriptor'):
            desc = binding.descriptor
            if hasattr(desc, 'resource'):
                rid = int(desc.resource)
                if rid != 0:
                    return rid
            # Fallback: descriptor.view
            if hasattr(desc, 'view'):
                rid = int(desc.view)
                if rid != 0:
                    return rid

        # --- Strategy 2: Direct .resourceId attribute (D3D11/GL style) ---
        if hasattr(binding, 'resourceId'):
            rid = int(binding.resourceId)
            if rid != 0:
                return rid

        # --- Strategy 3: .resources list (BoundResourceArray — D3D11/D3D12) ---
        if hasattr(binding, 'resources'):
            for d in binding.resources:
                if hasattr(d, 'resourceId'):
                    rid = int(d.resourceId)
                    if rid != 0:
                        return rid
                elif hasattr(d, 'resource'):
                    rid = int(d.resource)
                    if rid != 0:
                        return rid

        # --- Strategy 4: Direct .resource on binding (some RenderDoc versions) ---
        # Note: for Vulkan UsedDescriptor, int(binding.resource) returns a pointer, not ResourceId
        # Only try this if it looks like a real ResourceId (< 10M)
        if hasattr(binding, 'resource'):
            try:
                rid = int(binding.resource)
                if 0 < rid < 10000000:  # Reasonable ResourceId range
                    return rid
            except (TypeError, ValueError):
                pass

        # --- Strategy 5: D3D12 root signature / descriptor table ---
        if hasattr(binding, 'tableIndex') or hasattr(binding, 'rootElement'):
            if hasattr(binding, 'resource'):
                rid = int(binding.resource)
                if rid != 0:
                    return rid

    except Exception:
        pass

    return 0


def _resolve_vulkan_descriptors(ctrl, ps, stage, refl):
    """Resolve Vulkan descriptor set bindings to actual resource IDs.

    Vulkan uses descriptor sets which are not always resolvable through
    GetReadOnlyResources. This function uses GetDescriptorLocations (if available)
    or enumerates all used descriptors to map binding points to resource IDs.

    Returns dict: {binding_number: resource_id}
    """
    result = {}

    # Method 1: Try GetDescriptorAccess on the replay controller
    try:
        accesses = ctrl.GetDescriptorAccess()
        if accesses:
            for a in accesses:
                bind = getattr(a, 'index', getattr(a, 'binding', -1))
                dset = getattr(a, 'descriptorSet', getattr(a, 'set', -1))
                rid = 0
                if hasattr(a, 'resource'):
                    rid = int(a.resource)
                elif hasattr(a, 'resourceId'):
                    rid = int(a.resourceId)
                if rid != 0 and bind >= 0:
                    result[bind] = rid
            if result:
                return result
    except Exception:
        pass

    # Method 2: Try Vulkan pipeline state descriptor sets
    try:
        vk_state = None
        if hasattr(ps, 'vulkan'):
            vk_state = ps.vulkan
        elif hasattr(ps, 'GetVulkanPipelineState'):
            vk_state = ps.GetVulkanPipelineState()

        if vk_state and hasattr(vk_state, 'graphics') and hasattr(vk_state.graphics, 'descriptorSets'):
            desc_sets = vk_state.graphics.descriptorSets
            for ds in desc_sets:
                bindings_list = getattr(ds, 'bindings', [])
                for b in bindings_list:
                    bind_num = getattr(b, 'binding', getattr(b, 'descriptorIndex', -1))
                    descriptors = getattr(b, 'binds', getattr(b, 'descriptors', []))
                    for d in descriptors:
                        rid = 0
                        if hasattr(d, 'resource'):
                            rid = int(d.resource)
                        elif hasattr(d, 'resourceId'):
                            rid = int(d.resourceId)
                        elif hasattr(d, 'viewResourceId'):
                            rid = int(d.viewResourceId)
                        elif hasattr(d, 'imageInfo'):
                            img = d.imageInfo
                            if hasattr(img, 'imageView'):
                                rid = int(img.imageView)
                        if rid != 0 and bind_num >= 0:
                            result[bind_num] = rid
                            break  # First valid descriptor in array
    except Exception:
        pass

    # Method 3: Try to get resource usage to find which textures were sampled
    if not result:
        try:
            textures = ctrl.GetTextures()
            for tex in textures:
                try:
                    usages = ctrl.GetUsage(tex.resourceId)
                    for u in usages:
                        eid = getattr(u, 'eventId', getattr(u, 'eventID', 0))
                        if eid == 0:
                            continue
                        # Check if this is a read in this event (fragment shader sample)
                        usage_type = str(getattr(u, 'usage', ''))
                        if 'read' in usage_type.lower() or 'sample' in usage_type.lower() \
                                or 'input' in usage_type.lower() or 'fs' in usage_type.lower():
                            rid = int(tex.resourceId)
                            # We don't know the exact binding, use negative slot as fallback
                            if rid not in result.values():
                                next_slot = len(result)
                                result[next_slot] = rid
                except Exception:
                    continue
        except Exception:
            pass

    return result


_ROLE_PATTERNS = [
    (["albedo", "diffuse", "basecolor", "base_color", "color", "maintex", "_col"], "albedo"),
    (["normal", "nrm", "bump", "normalmap", "_n_", "_norm"], "normal"),
    (["metallic", "metalness", "_met", "metal"], "metallic"),
    (["rough", "roughness", "_rgh", "smoothness"], "roughness"),
    (["ao", "occlusion", "_occ"], "ao"),
    (["emissive", "emission", "glow"], "emissive"),
    (["specular", "spec"], "specular"),
    (["height", "displacement", "parallax"], "height"),
    (["opacity", "alpha", "mask"], "opacity"),
    (["shadow"], "shadow"),
    (["env", "cubemap", "skybox", "reflection", "ibl"], "environment"),
    (["lightmap", "lm_"], "lightmap"),
    (["depth", "zbuffer"], "depth"),
]


def _infer_role(var_name, tex_name, tex_format):
    combined = (var_name + " " + tex_name).lower()
    for keywords, role in _ROLE_PATTERNS:
        for kw in keywords:
            if kw in combined:
                return role
    fmt = tex_format.lower()
    if "bc5" in fmt:
        return "normal"
    if "bc6" in fmt:
        return "environment"
    return "unknown"


def _get_frame_summary(params):
    """Get frame summary stats."""
    return _get_capture_status(params)


def _get_draw_call_details(params):
    """Get details of a specific draw call."""
    event_id = params.get("event_id")
    if event_id is None:
        raise ValueError("event_id required")

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()
        pid = _get_pipeline_obj(ps, ctrl)

        result = {"eventId": int(event_id)}
        for name, stage in [("vertex", rd.ShaderStage.Vertex), ("fragment", rd.ShaderStage.Fragment)]:
            sid = ps.GetShader(stage)
            if sid and int(sid) != 0:
                result[name + "Shader"] = int(sid)
        targets = ps.GetOutputTargets()
        result["renderTargets"] = [int(t.resource) for t in targets if int(t.resource) != 0]
        depth = ps.GetDepthTarget()
        result["depthTarget"] = int(depth.resource) if depth and int(depth.resource) != 0 else None
        try:
            vp = ps.GetViewport(0)
            if vp:
                result["viewport"] = {"x": vp.x, "y": vp.y, "w": vp.width, "h": vp.height}
        except Exception:
            pass
        return result

    return _replay(_do)


def _get_texture_info(params):
    """Get single texture info."""
    resource_id = params.get("resource_id")
    if resource_id is None:
        raise ValueError("resource_id required")

    def _do(ctrl):
        name_lookup = {}
        for r in ctrl.GetResources():
            name_lookup[int(r.resourceId)] = r.name
        for t in ctrl.GetTextures():
            if int(t.resourceId) == int(resource_id):
                return {
                    "id": int(t.resourceId), "name": name_lookup.get(int(t.resourceId), ""),
                    "w": t.width, "h": t.height, "d": t.depth,
                    "fmt": _fmt_str(t.format), "mips": t.mips,
                    "array": t.arraysize, "size": t.byteSize,
                }
        return {"error": "Texture not found"}

    return _replay(_do)


def _get_texture_data(params):
    """Get texture pixel data as base64."""
    import base64
    resource_id = params.get("resource_id")
    if resource_id is None:
        raise ValueError("resource_id required")
    mip = params.get("mip", 0)
    slc = params.get("slice", 0)

    def _do(ctrl):
        tid = rd.ResourceId()
        tid.id = int(resource_id)
        sub = rd.Subresource(mip, slc, 0)
        data = ctrl.GetTextureData(tid, sub)
        return {"resourceId": int(resource_id), "length": len(data), "base64": base64.b64encode(data).decode("ascii")[:10000]}

    return _replay(_do)


def _get_buffer_data(params):
    """Get buffer contents."""
    import base64
    resource_id = params.get("resource_id")
    if resource_id is None:
        raise ValueError("resource_id required")
    offset = params.get("offset", 0)
    length = params.get("length", 256)

    def _do(ctrl):
        bid = rd.ResourceId()
        bid.id = int(resource_id)
        data = ctrl.GetBufferData(bid, offset, length)
        return {"resourceId": int(resource_id), "offset": offset, "length": len(data),
                "hex": data[:512].hex(), "base64": base64.b64encode(data[:4096]).decode("ascii")}

    return _replay(_do)


def _pick_pixel(params):
    """Pick a pixel value from a texture."""
    resource_id = params.get("resource_id")
    x = params.get("x", 0)
    y = params.get("y", 0)
    if resource_id is None:
        raise ValueError("resource_id required")

    def _do(ctrl):
        tid = rd.ResourceId()
        tid.id = int(resource_id)
        sub = rd.Subresource(0, 0, 0)
        val = ctrl.PickPixel(tid, x, y, sub, rd.CompType.Typeless)
        return {"x": x, "y": y, "r": val.floatValue[0], "g": val.floatValue[1],
                "b": val.floatValue[2], "a": val.floatValue[3]}

    return _replay(_do)


def _get_texture_minmax(params):
    """Get min/max pixel values of a texture."""
    resource_id = params.get("resource_id")
    if resource_id is None:
        raise ValueError("resource_id required")

    def _do(ctrl):
        tid = rd.ResourceId()
        tid.id = int(resource_id)
        sub = rd.Subresource(0, 0, 0)
        mn, mx = ctrl.GetMinMax(tid, sub, rd.CompType.Typeless)
        return {
            "min": {"r": mn.floatValue[0], "g": mn.floatValue[1], "b": mn.floatValue[2], "a": mn.floatValue[3]},
            "max": {"r": mx.floatValue[0], "g": mx.floatValue[1], "b": mx.floatValue[2], "a": mx.floatValue[3]},
        }

    return _replay(_do)


def _pixel_history(params):
    """Get pixel modification history."""
    resource_id = params.get("resource_id")
    x = params.get("x", 0)
    y = params.get("y", 0)
    if resource_id is None:
        raise ValueError("resource_id required")

    def _do(ctrl):
        tid = rd.ResourceId()
        tid.id = int(resource_id)
        sub = rd.Subresource(0, 0, 0)
        mods = ctrl.PixelHistory(tid, x, y, sub, rd.CompType.Typeless)
        result = []
        for m in mods:
            entry = {"eventId": m.eventId}
            try:
                if m.preMod:
                    entry["pre"] = {"r": m.preMod.col.floatValue[0], "g": m.preMod.col.floatValue[1],
                                    "b": m.preMod.col.floatValue[2], "a": m.preMod.col.floatValue[3]}
                if m.postMod:
                    entry["post"] = {"r": m.postMod.col.floatValue[0], "g": m.postMod.col.floatValue[1],
                                     "b": m.postMod.col.floatValue[2], "a": m.postMod.col.floatValue[3]}
            except Exception:
                pass
            result.append(entry)
        return result

    return _replay(_do)


def _debug_pixel(params):
    """Debug pixel shader execution."""
    x = params.get("x", 0)
    y = params.get("y", 0)

    def _do(ctrl):
        inputs = rd.DebugPixelInputs()
        inputs.sample = 0
        inputs.primitive = 0xFFFFFFFF
        trace = ctrl.DebugPixel(x, y, inputs)
        if not trace or not trace.debugger:
            return {"error": "No debug trace for this pixel"}
        states = []
        batch = ctrl.ContinueDebug(trace.debugger)
        while batch:
            for s in batch:
                step = {"step": s.stepIndex}
                if s.sourceVars:
                    step["vars"] = [{"name": v.name, "val": str(v.value)} for v in s.sourceVars[:10]]
                states.append(step)
            batch = ctrl.ContinueDebug(trace.debugger)
        ctrl.FreeTrace(trace)
        return {"x": x, "y": y, "steps": len(states), "trace": states[:50]}

    return _replay(_do)


def _debug_vertex(params):
    """Debug vertex shader execution."""
    vertex_id = params.get("vertex_id", 0)
    instance_id = params.get("instance_id", 0)

    def _do(ctrl):
        trace = ctrl.DebugVertex(vertex_id, instance_id, 0, 0)
        if not trace or not trace.debugger:
            return {"error": "No debug trace for this vertex"}
        states = []
        batch = ctrl.ContinueDebug(trace.debugger)
        while batch:
            for s in batch:
                step = {"step": s.stepIndex}
                if s.sourceVars:
                    step["vars"] = [{"name": v.name, "val": str(v.value)} for v in s.sourceVars[:10]]
                states.append(step)
            batch = ctrl.ContinueDebug(trace.debugger)
        ctrl.FreeTrace(trace)
        return {"vertexId": vertex_id, "steps": len(states), "trace": states[:50]}

    return _replay(_do)


def _enumerate_counters(params):
    """List available GPU counters."""
    def _do(ctrl):
        counters = ctrl.EnumerateCounters()
        result = []
        for c in counters:
            desc = ctrl.DescribeCounter(c)
            result.append({"id": int(c), "name": desc.name, "desc": desc.description, "unit": str(desc.unit)})
        return result
    return _replay(_do)


def _fetch_counters(params):
    """Fetch GPU counter values."""
    ids = params.get("counter_ids", [])
    if not ids:
        raise ValueError("counter_ids required")

    def _do(ctrl):
        counters = [rd.GPUCounter(c) for c in ids]
        results = ctrl.FetchCounters(counters)
        return [{"eventId": r.eventId, "counter": int(r.counter), "value": r.value.d if hasattr(r.value, "d") else 0} for r in results]
    return _replay(_do)


def _get_post_vs_data(params):
    """Get post-VS mesh data."""
    def _do(ctrl):
        mesh = ctrl.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
        return {"numIndices": mesh.numIndices, "topology": str(mesh.topology),
                "indexStride": mesh.indexByteStride, "vertexStride": mesh.vertexByteStride}
    return _replay(_do)


def _get_debug_messages(params):
    """Get debug/validation messages."""
    def _do(ctrl):
        msgs = ctrl.GetDebugMessages()
        return [{"eventId": m.eventId, "severity": str(m.severity), "msg": m.description} for m in msgs]
    return _replay(_do)


def _list_captures(params):
    """List .rdc files in a directory."""
    directory = params.get("directory", "")
    if not directory:
        raise ValueError("directory required")
    result = []
    for f in os.listdir(directory):
        if f.lower().endswith(".rdc"):
            fp = os.path.join(directory, f)
            result.append({"name": f, "path": fp, "size": os.path.getsize(fp)})
    return result


def _open_capture(params):
    """Open a capture file in RenderDoc."""
    path = params.get("capture_path", "")
    if not path:
        raise ValueError("capture_path required")
    if not _ctx:
        return {"error": "No context"}
    # LoadCapture signature varies by RenderDoc version. RenderDoc 1.44 uses:
    # LoadCapture(filename, ReplayOptions, origFilename, temporary, local)
    try:
        _ctx.LoadCapture(path, rd.ReplayOptions(), path, False, True)
    except TypeError:
        try:
            _ctx.LoadCapture(path, "", False, True)
        except TypeError:
            _ctx.LoadCapture(path, "", False, True, True)
    return {"opened": path}


def _get_bound_textures(params):
    """Get textures bound to a shader stage with role inference."""
    event_id = params.get("event_id")
    stage_name = params.get("stage", "fragment")
    if event_id is None:
        raise ValueError("event_id required")

    stage_map = {
        "vertex": rd.ShaderStage.Vertex, "fragment": rd.ShaderStage.Fragment,
        "pixel": rd.ShaderStage.Fragment, "geometry": rd.ShaderStage.Geometry,
        "compute": rd.ShaderStage.Compute,
        "hull": rd.ShaderStage.Hull, "domain": rd.ShaderStage.Domain,
        "tess_ctrl": rd.ShaderStage.Hull, "tess_eval": rd.ShaderStage.Domain,
    }
    stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()
        sid = ps.GetShader(stage)
        if not sid or int(sid) == 0:
            return []
        pid = _get_pipeline_obj(ps, ctrl)
        entries = ctrl.GetShaderEntryPoints(sid)
        if not entries:
            return []
        refl = ctrl.GetShader(pid, sid, entries[0])
        if not refl:
            return []

        name_lookup = {}
        for r in ctrl.GetResources():
            name_lookup[int(r.resourceId)] = r.name
        tex_lookup = {}
        for t in ctrl.GetTextures():
            tex_lookup[int(t.resourceId)] = t

        try:
            ro_bindings = ps.GetReadOnlyResources(stage)
        except Exception:
            ro_bindings = None

        # Pre-resolve Vulkan descriptors as fallback
        vk_desc = {}
        try:
            vk_desc = _resolve_vulkan_descriptors(ctrl, ps, stage, refl)
        except Exception:
            pass

        result = []
        for i, ro in enumerate(getattr(refl, 'readOnlyResources', [])):
            if not getattr(ro, 'isTexture', False):
                continue
            ro_name = getattr(ro, 'name', 'tex%d' % i)
            bind_num = getattr(ro, 'fixedBindNumber', i)
            entry = {"slot": i, "name": ro_name, "bind": bind_num, "resourceId": 0}
            # Resolve bound resource — compatible with D3D11/D3D12/Vulkan/GL
            entry["resourceId"] = _resolve_bound_resource(ro_bindings, i, ctrl)
            # Fallback: try Vulkan descriptor resolution by binding number
            if entry["resourceId"] == 0 and bind_num in vk_desc:
                entry["resourceId"] = vk_desc[bind_num]
            # Fallback: try by slot index
            if entry["resourceId"] == 0 and i in vk_desc:
                entry["resourceId"] = vk_desc[i]
            rid = entry["resourceId"]
            if rid and rid in tex_lookup:
                t = tex_lookup[rid]
                entry["texName"] = name_lookup.get(rid, "")
                entry["width"] = t.width
                entry["height"] = t.height
                entry["format"] = _fmt_str(t.format)
                entry["role"] = _infer_role(ro_name, name_lookup.get(rid, ""), _fmt_str(t.format))
            else:
                entry["role"] = _infer_role(ro_name, "", "")
            result.append(entry)
        return result

    return _replay(_do)


def _reverse_shader(params):
    """Full shader reverse-engineering."""
    event_id = params.get("event_id")
    stage_name = params.get("stage", "fragment")
    if event_id is None:
        raise ValueError("event_id required")

    # Reuse get_shader_info which already does full reverse
    return _get_shader_info({"event_id": event_id, "stage": stage_name})


def _analyze_lighting(params):
    """Analyze lighting setup from a draw call's shader and constant buffers."""
    event_id = params.get("event_id")
    if event_id is None:
        raise ValueError("event_id required")

    def _do(ctrl):
        try:
            ctrl.SetFrameEvent(int(event_id), True)
            ps = ctrl.GetPipelineState()
            pid = _get_pipeline_obj(ps, ctrl)
            result = {"eventId": int(event_id)}
        except Exception as e:
            return {"error": "init: " + str(e)}

        # 1. Get shader reflection
        for stage_name, stage_enum in [("fragment", rd.ShaderStage.Fragment), ("vertex", rd.ShaderStage.Vertex)]:
            try:
                sid = ps.GetShader(stage_enum)
                if not sid or int(sid) == 0:
                    continue

                entries = ctrl.GetShaderEntryPoints(sid)
                if not entries:
                    continue
                refl = ctrl.GetShader(pid, sid, entries[0])
                if not refl:
                    continue
            except Exception as e:
                result["shaderError_%s" % stage_name] = str(e)[:200]
                continue

            # 2. Extract cbuffer variable values
            cbuffers_info = []

            # Get cbuffer bindings
            cbufs_bound = None
            try:
                cbufs_bound = ps.GetConstantBuffers(stage_enum, False)
            except Exception:
                pass  # Vulkan: no GetConstantBuffers, use null resource fallback

            for cb_idx, cb in enumerate(getattr(refl, 'constantBlocks', [])):
                cb_name = getattr(cb, 'name', 'cbuffer%d' % cb_idx)
                cb_info = {"name": cb_name, "index": cb_idx, "stage": stage_name}

                try:
                    buf_vars = None
                    # Try with bound resource (8-arg version)
                    if cbufs_bound and cb_idx < len(cbufs_bound):
                        b = cbufs_bound[cb_idx]
                        b_rid = rd.ResourceId()
                        if hasattr(b, 'descriptor') and hasattr(b.descriptor, 'resource'):
                            b_rid = b.descriptor.resource
                        elif hasattr(b, 'resource'):
                            b_rid = b.resource
                        try:
                            buf_vars = ctrl.GetCBufferVariableContents(
                                pid, sid, stage_enum, entries[0].name,
                                cb_idx, b_rid,
                                getattr(b, 'byteOffset', 0),
                                getattr(b, 'byteSize', 0))
                        except Exception:
                            pass

                    # Fallback with null resource
                    if not buf_vars:
                        try:
                            null_rid = rd.ResourceId()
                            buf_vars = ctrl.GetCBufferVariableContents(
                                pid, sid, stage_enum, entries[0].name,
                                cb_idx, null_rid, 0, 0)
                        except Exception:
                            pass

                    if buf_vars:
                        import struct as _struct_cb

                        # Try to read raw buffer data for value extraction
                        raw_data = None
                        buf_debug = {}
                        if cbufs_bound and cb_idx < len(cbufs_bound):
                            b = cbufs_bound[cb_idx]
                            b_rid = None
                            if hasattr(b, 'descriptor') and hasattr(b.descriptor, 'resource'):
                                b_rid = b.descriptor.resource
                            elif hasattr(b, 'resource'):
                                try:
                                    if 0 < int(b.resource) < 10000000:
                                        b_rid = b.resource
                                except Exception:
                                    pass
                            if b_rid and int(b_rid) != 0:
                                try:
                                    raw_data = ctrl.GetBufferData(b_rid, 0, 0)
                                    buf_debug["rid"] = int(b_rid)
                                    buf_debug["rawBytes"] = len(raw_data) if raw_data else 0
                                except Exception:
                                    pass

                        # If raw buffer data available, dump all float4 values
                        if raw_data and len(raw_data) >= 16:
                            float4s = []
                            for off in range(0, min(len(raw_data), 2048) - 15, 16):
                                try:
                                    x, y, z, w = _struct_cb.unpack_from("<ffff", raw_data, off)
                                    float4s.append([round(x, 6), round(y, 6), round(z, 6), round(w, 6)])
                                except Exception:
                                    break
                            cb_info["rawFloat4s"] = float4s
                            cb_info["rawFloat4Count"] = len(float4s)

                        cb_info["bufferDebug"] = buf_debug

                        light_vars = []
                        shadow_vars = []
                        ambient_vars = []
                        all_vars = []

                        for v in buf_vars:
                            vd = _var_dict(v)
                            vname = vd.get("name", "").lower()

                            # Extract values from raw buffer if _var_dict didn't get them
                            if "float" not in vd and raw_data and hasattr(v, 'byteOffset'):
                                off = v.byteOffset
                                cols = getattr(v, 'columns', 0)
                                rows = getattr(v, 'rows', 0)
                                count = max(cols, 1) * max(rows, 1)
                                if count > 0 and off + count * 4 <= len(raw_data):
                                    try:
                                        vals = list(_struct_cb.unpack_from("<%df" % count, raw_data, off))
                                        # Trim trailing zeros
                                        while vals and vals[-1] == 0.0:
                                            vals.pop()
                                        if vals:
                                            vd["float"] = [round(x, 6) for x in vals]
                                    except Exception:
                                        pass

                            # Also extract member values from raw buffer
                            if "members" in vd and raw_data:
                                for mi, m in enumerate(vd["members"]):
                                    if "float" not in m:
                                        mv = v.members[mi] if hasattr(v, 'members') and mi < len(v.members) else None
                                        if mv and hasattr(mv, 'byteOffset'):
                                            moff = mv.byteOffset
                                            mcols = getattr(mv, 'columns', 4)
                                            mrows = getattr(mv, 'rows', 1)
                                            mcount = max(mcols, 1) * max(mrows, 1)
                                            if mcount > 0 and moff + mcount * 4 <= len(raw_data):
                                                try:
                                                    mvals = list(_struct_cb.unpack_from("<%df" % mcount, raw_data, moff))
                                                    while mvals and mvals[-1] == 0.0:
                                                        mvals.pop()
                                                    if mvals:
                                                        m["float"] = [round(x, 6) for x in mvals]
                                                except Exception:
                                                    pass

                            vals = vd.get("float", [])

                            # Auto-classify by variable name
                            category = None
                            if any(k in vname for k in ['light', 'sun', 'direction', 'dir']):
                                category = "light"
                            elif any(k in vname for k in ['shadow', 'cascade', 'bias']):
                                category = "shadow"
                            elif any(k in vname for k in ['ambient', 'env', 'sh', 'ibl', 'probe', 'gi']):
                                category = "ambient"
                            elif any(k in vname for k in ['color', 'colour', 'intensity',
                                                           'atten', 'range', 'radius', 'falloff']):
                                category = "light_param"

                            # Heuristic for anonymous ANGLE variables (_childN)
                            if category is None and vname.startswith('_child'):
                                has_members = "members" in vd
                                member_count = len(vd.get("members", []))

                                # float4[4] = likely SH coefficients or shadow matrix
                                if member_count == 4:
                                    category = "ambient_or_matrix"
                                # float4[6] = likely light array (pos/color/dir/atten per light)
                                elif member_count == 6:
                                    category = "light_array"
                                # float4[8] = likely shadow cascade matrix
                                elif member_count == 8:
                                    category = "shadow_matrix"
                                # float4[2] = likely shadow atlas UV
                                elif member_count == 2:
                                    category = "shadow_param"
                                # Scalar/vector by value heuristic
                                elif not has_members and vals:
                                    if len(vals) >= 3:
                                        mag = sum(v2*v2 for v2 in vals[:3]) ** 0.5
                                        if 0.95 < mag < 1.05:
                                            category = "light_direction"
                                        elif all(0 <= v2 <= 2.0 for v2 in vals[:3]) and sum(vals[:3]) > 0.01:
                                            category = "color_or_light"

                            if category:
                                vd["_category"] = category
                                if "light" in category:
                                    light_vars.append(vd)
                                elif "shadow" in category:
                                    shadow_vars.append(vd)
                                elif "ambient" in category:
                                    ambient_vars.append(vd)

                            all_vars.append(vd)

                        cb_info["varCount"] = len(all_vars)
                        cb_info["variables"] = all_vars[:60]
                        if light_vars:
                            cb_info["lightVars"] = light_vars
                        if shadow_vars:
                            cb_info["shadowVars"] = shadow_vars
                        if ambient_vars:
                            cb_info["ambientVars"] = ambient_vars
                    else:
                        cb_info["note"] = "no variables returned"
                except Exception as e:
                    cb_info["error"] = str(e)[:200]

                cbuffers_info.append(cb_info)

            if cbuffers_info:
                result.setdefault("cbuffers", {})[stage_name] = cbuffers_info

            # 3. Detect lighting model from shader disassembly
            if stage_name == "fragment":
                try:
                    targets = ctrl.GetDisassemblyTargets(True)
                    spirv_target = None
                    for t in targets:
                        if 'SPIR-V' in t:
                            spirv_target = t
                            break
                    if spirv_target:
                        code = ctrl.DisassembleShader(pid, refl, spirv_target)
                        if code:
                            result["lightingModel"] = _detect_lighting_model(code)
                except Exception as e:
                    result["lightingModelError"] = str(e)[:200]

            # 4. Identify shadow maps and env maps from bound textures
            try:
                tex_lookup = {}
                for t in ctrl.GetTextures():
                    tex_lookup[int(t.resourceId)] = t

                try:
                    ro_bindings = ps.GetReadOnlyResources(stage_enum)
                except Exception:
                    ro_bindings = None

                shadow_maps = []
                env_maps = []

                for i, ro in enumerate(getattr(refl, 'readOnlyResources', [])):
                    if not getattr(ro, 'isTexture', False):
                        continue
                    ro_name = getattr(ro, 'name', 'tex%d' % i)
                    bound_rid = _resolve_bound_resource(ro_bindings, i, ctrl)
                    tex = tex_lookup.get(bound_rid)
                    if not tex:
                        continue

                    fmt = _fmt_str(tex.format)
                    is_depth = any(k in fmt.lower() for k in ['depth', 'd32', 'd24', 'd16'])
                    is_cubemap = tex.arraysize == 6

                    entry = {"slot": i, "name": ro_name, "resourceId": bound_rid,
                             "width": tex.width, "height": tex.height, "format": fmt}

                    if is_depth:
                        entry["role"] = "shadow_map"
                        shadow_maps.append(entry)
                    elif is_cubemap:
                        entry["role"] = "environment_cubemap"
                        env_maps.append(entry)

                if shadow_maps:
                    result["shadowMaps"] = shadow_maps
                if env_maps:
                    result["environmentMaps"] = env_maps
            except Exception as e:
                result["textureAnalysisError_%s" % stage_name] = str(e)[:200]

        return result

    return _replay(_do)


def _detect_lighting_model(code):
    """Detect lighting model from shader disassembly."""
    info = {"model": "unknown", "features": []}
    code_lower = code.lower() if code else ""

    # PBR indicators
    if any(k in code_lower for k in ['ggx', 'brdf', 'metallic', 'roughness', 'pbr',
                                       'cooktorrance', 'smith', 'schlick', 'beckmann']):
        info["model"] = "PBR"
    elif any(k in code_lower for k in ['blinnphong', 'blinn', 'phong', 'specular_power']):
        info["model"] = "Blinn-Phong"
    elif any(k in code_lower for k in ['lambert', 'diffuse_only']):
        info["model"] = "Lambert"
    elif 'inversesqrt' in code_lower and 'dot' in code_lower:
        info["model"] = "PBR (inferred)"  # GGX uses InverseSqrt + Dot patterns

    # Feature detection
    if 'shadow' in code_lower or 'drefexplicitlod' in code_lower or 'shadowmap' in code_lower:
        info["features"].append("shadow_mapping")
    if any(k in code_lower for k in ['cascade', 'csm']):
        info["features"].append("cascaded_shadow_maps")
    if any(k in code_lower for k in ['subsurface', 'sss', 'scattering']):
        info["features"].append("subsurface_scattering")
    if any(k in code_lower for k in ['fresnel', 'schlick']):
        info["features"].append("fresnel")
    if any(k in code_lower for k in ['envmap', 'cubemap', 'reflection', 'imagesampleexplicitlod']):
        info["features"].append("environment_reflection")
    if 'ao' in code_lower or 'occlusion' in code_lower:
        info["features"].append("ambient_occlusion")
    if any(k in code_lower for k in ['emissive', 'emission', 'glow', 'bloom']):
        info["features"].append("emissive")
    if any(k in code_lower for k in ['normalmap', 'tangent', 'bitangent', 'tbn']):
        info["features"].append("normal_mapping")

    # Count light loop iterations (look for while/for patterns with light index)
    if 'while' in code_lower and ('child13' in code_lower or 'child14' in code_lower or 'light' in code_lower):
        info["features"].append("multi_light_loop")

    return info


def _parse_light_variables(buf_vars):
    """Parse cbuffer variables and extract lighting-related values."""
    result = {"directionalLights": [], "pointLights": [], "parameters": []}

    for v in buf_vars:
        var_data = _var_dict(v)
        name = var_data.get("name", "")

        # Try to extract float values
        vals = None
        if "value" in var_data:
            val = var_data["value"]
            if isinstance(val, (list, tuple)):
                vals = [float(x) for x in val if isinstance(x, (int, float))]
            elif isinstance(val, dict):
                for k in ["f32v", "f", "x", "r"]:
                    if k in val:
                        v2 = val[k]
                        if isinstance(v2, (list, tuple)):
                            vals = [float(x) for x in v2[:4]]
                        break

        if vals:
            var_data["floatValues"] = vals[:4]

        result["parameters"].append(var_data)

    return result


def _analyze_cbuffer_lighting(cb_data, cb_name, cb_bind, stage_name):
    """Heuristic analysis of raw cbuffer data for lighting parameters."""
    import struct as _struct

    info = {"name": cb_name, "binding": cb_bind, "stage": stage_name, "size": len(cb_data)}

    # Read all float4 values
    float4s = []
    for off in range(0, len(cb_data) - 15, 16):
        try:
            x, y, z, w = _struct.unpack_from("<ffff", cb_data, off)
            float4s.append({"offset": off, "values": [x, y, z, w]})
        except Exception:
            break

    # Heuristic: identify potential light directions (unit vectors)
    directions = []
    colors = []
    matrices = []

    for f4 in float4s:
        x, y, z, w = f4["values"]
        mag = (x * x + y * y + z * z) ** 0.5

        # Unit vector = likely direction
        if 0.95 < mag < 1.05 and abs(w) < 0.1:
            directions.append({"offset": f4["offset"], "direction": [round(x, 4), round(y, 4), round(z, 4)],
                               "hint": "possible light direction"})

        # RGB color (0-1 range, positive)
        elif 0 <= x <= 5 and 0 <= y <= 5 and 0 <= z <= 5 and (x + y + z) > 0.01:
            if x != y or y != z:  # Not a scalar
                colors.append({"offset": f4["offset"], "color": [round(x, 4), round(y, 4), round(z, 4)],
                                "w": round(w, 4), "hint": "possible light color"})

    info["potentialDirections"] = directions[:10]
    info["potentialColors"] = colors[:20]
    info["totalFloat4s"] = len(float4s)

    return info


def _identify_drawcalls(params):
    """Identify DrawCall content by rendering RT thumbnails for each action.

    For each DrawCall in the specified range, captures the render target BEFORE and AFTER
    the draw, computes a pixel diff, and saves a cropped thumbnail showing ONLY what that
    DrawCall contributed. Also collects shader IDs, bound texture info, and vertex AABB.

    Args:
        event_id_min: Start of event range (default 0).
        event_id_max: End of event range (default 999999).
        output_dir: Directory to save per-DrawCall thumbnails (optional).
        render_target: Override RT resource ID (auto-detected if omitted).
    Returns:
        List of DrawCall identifications with thumbnails and metadata.
    """
    eid_min = params.get("event_id_min", 0)
    eid_max = params.get("event_id_max", 999999)
    output_dir = params.get("output_dir", "")
    rt_override = params.get("render_target", 0)

    def _do(ctrl):
        import struct as _struct

        # Get all draw actions in range
        all_actions = ctrl.GetRootActions()
        actions = []

        def _collect(act_list):
            for a in act_list:
                eid = a.eventId
                if eid < eid_min or eid > eid_max:
                    if a.children:
                        _collect(a.children)
                    continue
                flags = int(a.flags)
                # Check if it's an actual draw (has indices)
                if a.numIndices > 0 and (flags & 0x2):  # ActionFlags.Drawcall = 0x2
                    actions.append(a)
                if a.children:
                    _collect(a.children)

        _collect(all_actions)

        if not actions:
            return {"error": "No draw actions found in range %d-%d" % (eid_min, eid_max)}

        # Determine render target
        rt_id = rt_override
        if not rt_id:
            # Use the first action's output RT
            ctrl.SetFrameEvent(actions[0].eventId, True)
            ps = ctrl.GetPipelineState()
            outs = ps.GetOutputTargets()
            if outs:
                for o in outs:
                    rid = 0
                    if hasattr(o, 'resource'):
                        rid = int(o.resource)
                    elif hasattr(o, 'resourceId'):
                        rid = int(o.resourceId)
                    # Vulkan: UsedDescriptor style
                    if rid == 0 and hasattr(o, 'descriptor'):
                        desc = o.descriptor
                        if hasattr(desc, 'resource'):
                            rid = int(desc.resource)
                    if rid != 0:
                        rt_id = rid
                        break

        if not rt_id:
            return {"error": "Could not determine render target"}

        # Get RT dimensions
        tex_info = None
        for t in ctrl.GetTextures():
            if int(t.resourceId) == rt_id:
                tex_info = t
                break

        if not tex_info:
            return {"error": "Render target %d not found" % rt_id}

        rt_width = tex_info.width
        rt_height = tex_info.height

        # Create output directory
        if output_dir:
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

        results = []
        prev_pixels = None

        # For each action, capture RT state and diff
        for idx, action in enumerate(actions):
            eid = action.eventId
            entry = {
                "eventId": eid,
                "name": action.name if hasattr(action, 'name') else "Action",
                "numIndices": action.numIndices,
                "numInstances": action.numInstances,
                "triangles": action.numIndices // 3,
            }

            # Set event to JUST BEFORE this draw (previous event)
            if idx > 0:
                prev_eid = actions[idx - 1].eventId
            else:
                # Find the event just before this action
                prev_eid = max(1, eid - 1)

            # Capture RT AFTER this draw
            ctrl.SetFrameEvent(eid, True)

            # Get shader info
            ps = ctrl.GetPipelineState()
            vs_id = ps.GetShader(rd.ShaderStage.Vertex)
            fs_id = ps.GetShader(rd.ShaderStage.Fragment)
            entry["vertexShader"] = int(vs_id) if vs_id else 0
            entry["fragmentShader"] = int(fs_id) if fs_id else 0

            # Get bound texture count and first texture info
            pid = _get_pipeline_obj(ps, ctrl)
            tex_bindings = []
            try:
                refl = None
                if fs_id and int(fs_id) != 0:
                    ep = ctrl.GetShaderEntryPoints(fs_id)
                    if ep:
                        refl = ctrl.GetShader(pid, fs_id, ep[0])
                if refl:
                    ro_bindings = None
                    try:
                        ro_bindings = ps.GetReadOnlyResources(rd.ShaderStage.Fragment)
                    except Exception:
                        pass

                    tex_lookup = {}
                    for t2 in ctrl.GetTextures():
                        tex_lookup[int(t2.resourceId)] = t2

                    for i, ro in enumerate(getattr(refl, 'readOnlyResources', [])):
                        if not getattr(ro, 'isTexture', False):
                            continue
                        ro_name = getattr(ro, 'name', 'tex%d' % i)
                        bound_rid = _resolve_bound_resource(ro_bindings, i, ctrl)
                        t_info = tex_lookup.get(bound_rid)
                        tex_bindings.append({
                            "slot": i,
                            "name": ro_name,
                            "resourceId": bound_rid,
                            "width": t_info.width if t_info else 0,
                            "height": t_info.height if t_info else 0,
                        })
            except Exception:
                pass

            entry["textureCount"] = len(tex_bindings)
            entry["textures"] = tex_bindings[:5]  # First 5 only

            # Try to get vertex AABB from post-VS data
            try:
                mesh_out = ctrl.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
                if mesh_out and mesh_out.numIndices > 0:
                    vb_rid = mesh_out.vertexResourceId
                    vb_stride = mesh_out.vertexByteStride
                    vb_off = mesh_out.vertexByteOffset
                    n_verts = min(mesh_out.numIndices, 5000)
                    if vb_rid and int(vb_rid) != 0 and vb_stride >= 16:
                        vb_data = ctrl.GetBufferData(vb_rid, vb_off, n_verts * vb_stride)
                        if vb_data and len(vb_data) >= vb_stride:
                            xs, ys, zs = [], [], []
                            actual = min(n_verts, len(vb_data) // vb_stride)
                            for vi in range(actual):
                                base = vi * vb_stride
                                if base + 16 <= len(vb_data):
                                    x, y, z, w = _struct.unpack_from("<ffff", vb_data, base)
                                    if w != 0 and abs(x) < 1e6 and abs(y) < 1e6:
                                        # Clip space -> NDC -> screen
                                        ndc_x = x / w
                                        ndc_y = y / w
                                        screen_x = (ndc_x * 0.5 + 0.5) * rt_width
                                        screen_y = (0.5 - ndc_y * 0.5) * rt_height
                                        xs.append(screen_x)
                                        ys.append(screen_y)
                                        zs.append(z / w if w != 0 else 0)
                            if xs:
                                entry["screenBBox"] = {
                                    "minX": int(max(0, min(xs))),
                                    "minY": int(max(0, min(ys))),
                                    "maxX": int(min(rt_width, max(xs))),
                                    "maxY": int(min(rt_height, max(ys))),
                                    "depth": round(sum(zs) / len(zs), 4),
                                }
                                entry["screenCoverage"] = round(
                                    (max(xs) - min(xs)) * (max(ys) - min(ys)) / (rt_width * rt_height) * 100, 2
                                )
            except Exception:
                pass

            # Save RT thumbnail if output_dir specified
            if output_dir:
                thumb_path = os.path.join(output_dir, "eid_%04d.png" % eid)
                try:
                    save = rd.TextureSave()
                    save.resourceId = rd.ResourceId()
                    # Set the ID value
                    id_obj = save.resourceId
                    # Copy from the actual texture resourceId
                    for t3 in ctrl.GetTextures():
                        if int(t3.resourceId) == rt_id:
                            save.resourceId = t3.resourceId
                            break
                    save.mip = 0
                    save.destType = rd.FileType.PNG
                    ret = ctrl.SaveTexture(save, thumb_path)
                    if hasattr(ret, 'code') and ret.code == rd.ResultCode.Succeeded:
                        entry["thumbnail"] = thumb_path
                    else:
                        entry["thumbnailError"] = str(ret)[:100]
                except Exception as e2:
                    entry["thumbnailError"] = str(e2)[:100]

            results.append(entry)

        # Group by shader
        shader_groups = {}
        for r in results:
            key = "%d/%d" % (r["vertexShader"], r["fragmentShader"])
            if key not in shader_groups:
                shader_groups[key] = []
            shader_groups[key].append(r["eventId"])

        return {
            "renderTarget": rt_id,
            "rtSize": "%dx%d" % (rt_width, rt_height),
            "actionCount": len(results),
            "shaderGroups": shader_groups,
            "actions": results,
        }

    return _replay(_do)


def _debug_vulkan_bindings(params):
    """Debug tool: dump Vulkan descriptor set bindings raw structure."""
    event_id = params.get("event_id")
    stage_name = params.get("stage", "fragment")
    if event_id is None:
        raise ValueError("event_id required")

    stage_map = {
        "vertex": rd.ShaderStage.Vertex, "fragment": rd.ShaderStage.Fragment,
        "pixel": rd.ShaderStage.Fragment, "geometry": rd.ShaderStage.Geometry,
        "compute": rd.ShaderStage.Compute,
        "hull": rd.ShaderStage.Hull, "domain": rd.ShaderStage.Domain,
        "tess_ctrl": rd.ShaderStage.Hull, "tess_eval": rd.ShaderStage.Domain,
    }
    stage = stage_map.get(stage_name.lower(), rd.ShaderStage.Fragment)

    def _introspect(obj, depth=0, max_depth=3):
        """Recursively dump object attributes."""
        if depth >= max_depth:
            return str(obj)[:200]
        if obj is None:
            return None
        if isinstance(obj, (int, float, str, bool)):
            return obj
        # Try list-like first (before int conversion)
        try:
            if hasattr(obj, '__len__') and hasattr(obj, '__getitem__'):
                items = []
                for i in range(min(len(obj), 20)):
                    items.append(_introspect(obj[i], depth + 1, max_depth))
                return {"_list": items, "_len": len(obj)}
        except Exception:
            pass
        # For SWIG objects, dump all non-callable attributes
        result = {"_type": type(obj).__name__}
        attrs_found = False
        for attr in dir(obj):
            if attr.startswith('_'):
                continue
            try:
                val = getattr(obj, attr)
                if callable(val):
                    continue
                attrs_found = True
                result[attr] = _introspect(val, depth + 1, max_depth)
            except Exception as e:
                result[attr] = "ERR:%s" % str(e)[:100]
        # If no useful attrs found, try int/str fallback
        if not attrs_found:
            try:
                result["_int"] = int(obj)
            except (TypeError, ValueError):
                pass
            result["_str"] = str(obj)[:200]
        return result

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()
        info = {"eventId": int(event_id), "api": str(ctrl.GetAPIProperties().pipelineType)}

        # 1. GetReadOnlyResources
        try:
            ro = ps.GetReadOnlyResources(stage)
            ro_dump = []
            for i, b in enumerate(ro):
                ro_dump.append(_introspect(b, 0, 4))
            info["readOnlyResources"] = {"count": len(ro), "items": ro_dump[:15]}
        except Exception as e:
            info["readOnlyResources"] = {"error": str(e)}

        # 2. GetReadWriteResources
        try:
            rw = ps.GetReadWriteResources(stage)
            info["readWriteResourceCount"] = len(rw)
        except Exception as e:
            info["readWriteResources"] = {"error": str(e)}

        # 3. Try Vulkan-specific pipeline state
        pipe = None
        try:
            pipe = ps.GetVulkanPipelineState()
            info["hasVulkanState"] = True
        except Exception:
            info["hasVulkanState"] = False

        if pipe:
            # Dump descriptor sets
            try:
                desc_sets = pipe.graphics.descriptorSets if hasattr(pipe, 'graphics') else []
                ds_dump = []
                for ds_idx, ds in enumerate(desc_sets):
                    ds_info = _introspect(ds, 0, 5)
                    ds_dump.append(ds_info)
                info["descriptorSets"] = {"count": len(desc_sets), "sets": ds_dump[:5]}
            except Exception as e:
                info["descriptorSets"] = {"error": str(e)}

            # Dump top-level pipe attributes
            try:
                pipe_attrs = {}
                for attr in dir(pipe):
                    if attr.startswith('_'):
                        continue
                    try:
                        val = getattr(pipe, attr)
                        if callable(val):
                            continue
                        pipe_attrs[attr] = type(val).__name__
                    except Exception:
                        pass
                info["pipelineAttrs"] = pipe_attrs
            except Exception:
                pass

        # 4. Try GetDescriptorAccess
        try:
            accesses = ctrl.GetDescriptorAccess()
            acc_dump = []
            for a in accesses[:20]:
                acc_dump.append(_introspect(a, 0, 3))
            info["descriptorAccess"] = {"count": len(accesses), "items": acc_dump}
        except Exception as e:
            info["descriptorAccess"] = {"error": str(e), "type": type(e).__name__}

        # 5. Shader reflection readOnlyResources
        sid = ps.GetShader(stage)
        if sid and int(sid) != 0:
            pid = _get_pipeline_obj(ps, ctrl)
            entries = ctrl.GetShaderEntryPoints(sid)
            if entries:
                refl = ctrl.GetShader(pid, sid, entries[0])
                if refl:
                    ro_res = getattr(refl, 'readOnlyResources', [])
                    refl_dump = []
                    for i, r in enumerate(ro_res):
                        refl_dump.append(_introspect(r, 0, 3))
                    info["shaderReflection"] = {
                        "readOnlyResourceCount": len(ro_res),
                        "items": refl_dump[:15]
                    }

        return info

    return _replay(_do)


def _export_drawcall(params):
    """Export a drawcall's shaders + textures to disk."""
    event_id = params.get("event_id")
    output_dir = params.get("output_dir")
    if event_id is None or not output_dir:
        raise ValueError("event_id and output_dir required")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    result = {"eventId": int(event_id), "outputDir": output_dir, "files": []}

    for stage_name in ["vertex", "hull", "domain", "geometry", "fragment"]:
        try:
            info = _get_shader_info({"event_id": event_id, "stage": stage_name})
            if "error" in info:
                continue

            # Save disassembly
            if "disassembly" in info:
                for target, code in info["disassembly"].items():
                    if code and not code.startswith("Error"):
                        safe = target.replace(" ", "_").replace("/", "_")
                        fp = os.path.join(output_dir, "%s_%s.txt" % (stage_name, safe))
                        with open(fp, "w") as f:
                            f.write(code)
                        result["files"].append(fp)

            # Save source files
            if "sourceFiles" in info:
                for sf in info["sourceFiles"]:
                    safe = sf["filename"].replace("/", "_").replace("\\", "_")
                    fp = os.path.join(output_dir, "%s_src_%s" % (stage_name, safe))
                    with open(fp, "w") as f:
                        f.write(sf["contents"])
                    result["files"].append(fp)

            # Save bound textures as PNG (with CubeMap support)
            if "boundTextures" in info:
                for tex in info["boundTextures"]:
                    rid = tex.get("resourceId", 0)
                    if not rid:
                        continue
                    role = tex.get("role", "unknown")
                    safe_name = tex.get("name", "tex").replace(" ", "_")
                    fp = os.path.join(output_dir, "%s_slot%d_%s_%s.png" % (stage_name, tex["slot"], safe_name, role))
                    try:
                        save_result = _save_texture({"resource_id": rid, "output_path": fp})
                        # Handle CubeMap: multiple face files returned
                        if isinstance(save_result, dict):
                            if "savedFaces" in save_result:
                                for face in save_result["savedFaces"]:
                                    result["files"].append(face["path"])
                            elif "saved" in save_result:
                                result["files"].append(save_result["saved"])
                    except Exception:
                        pass
        except Exception as e:
            result.setdefault("errors", []).append("%s: %s" % (stage_name, str(e)))

    # Save summary JSON
    summary_fp = os.path.join(output_dir, "summary.json")
    with open(summary_fp, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    result["files"].append(summary_fp)

    return result


def _export_to_unity(params):
    """Export a draw call's mesh (OBJ), textures (PNG), and Unity material JSON.

    This is the one-stop tool for bringing a RenderDoc draw call into Unity:
    - Exports the mesh as .obj (with positions, normals, UVs, indices)
    - Exports all bound textures as .png with role-based filenames
    - Generates a Unity-compatible material definition JSON
    - Generates a shader property mapping for Unity Standard/URP/Custom shaders
    """
    import struct as _struct
    import base64 as _b64

    event_id = params.get("event_id")
    output_dir = params.get("output_dir")
    mesh_name = params.get("mesh_name", "exported_mesh")
    if event_id is None or not output_dir:
        raise ValueError("event_id and output_dir required")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    result = {
        "eventId": int(event_id),
        "outputDir": output_dir,
        "meshFile": None,
        "textures": [],
        "materialFile": None,
        "shaderInfo": None,
        "files": [],
    }

    def _do(ctrl):
        ctrl.SetFrameEvent(int(event_id), True)
        ps = ctrl.GetPipelineState()

        # ============================================================
        # 1. EXPORT MESH AS OBJ
        # ============================================================
        mesh_data = None
        try:
            mesh_out = ctrl.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
            if mesh_out and mesh_out.numIndices > 0:
                mesh_data = _extract_mesh_obj(ctrl, ps, mesh_out, mesh_name, output_dir, event_id=event_id)
                if mesh_data:
                    result["meshFile"] = mesh_data["objPath"]
                    result["files"].append(mesh_data["objPath"])
                    if "fbxPath" in mesh_data:
                        result["fbxFile"] = mesh_data["fbxPath"]
                        result["files"].append(mesh_data["fbxPath"])
                    result["meshStats"] = {
                        "vertices": mesh_data.get("vertexCount", 0),
                        "triangles": mesh_data.get("triangleCount", 0),
                        "hasNormals": mesh_data.get("hasNormals", False),
                        "hasUVs": mesh_data.get("hasUVs", False),
                        "method": mesh_data.get("method", "unknown"),
                    }
        except Exception as e:
            import traceback as _tb
            result["meshError"] = str(e) + "\n" + _tb.format_exc()
            print("[MCP Bridge] export_to_unity mesh error: %s" % str(e))
            _tb.print_exc()

        # ============================================================
        # 2. COLLECT BOUND TEXTURE INFO (no file export — use save_texture / export_drawcall for that)
        # ============================================================
        pid = _get_pipeline_obj(ps, ctrl)
        tex_exports = []

        for stage_name, stage_enum in [("fragment", rd.ShaderStage.Fragment), ("vertex", rd.ShaderStage.Vertex)]:
            sid = ps.GetShader(stage_enum)
            if not sid or int(sid) == 0:
                continue
            entries = ctrl.GetShaderEntryPoints(sid)
            if not entries:
                continue
            refl = ctrl.GetShader(pid, sid, entries[0])
            if not refl:
                continue

            name_lookup = {}
            for r in ctrl.GetResources():
                name_lookup[int(r.resourceId)] = r.name
            tex_lookup = {}
            for t in ctrl.GetTextures():
                tex_lookup[int(t.resourceId)] = t

            try:
                ro_bindings = ps.GetReadOnlyResources(stage_enum)
            except Exception:
                ro_bindings = None

            for i, ro in enumerate(getattr(refl, 'readOnlyResources', [])):
                if not getattr(ro, 'isTexture', False):
                    continue
                ro_name = getattr(ro, 'name', 'tex%d' % i)
                bound_rid = _resolve_bound_resource(ro_bindings, i, ctrl)

                if not bound_rid:
                    continue

                tex_info = tex_lookup.get(bound_rid, None)
                fmt_name = _fmt_str(tex_info.format) if tex_info else "unknown"
                role = _infer_role(ro_name, name_lookup.get(bound_rid, ""), fmt_name)

                tex_entry = {
                    "role": role,
                    "slot": i,
                    "stage": stage_name,
                    "shaderVar": ro_name,
                    "resourceId": bound_rid,
                    "width": tex_info.width if tex_info else 0,
                    "height": tex_info.height if tex_info else 0,
                    "format": fmt_name,
                    "hint": "Use save_texture(resource_id=%d, output_path=...) to export" % bound_rid,
                }
                tex_exports.append(tex_entry)

        result["textures"] = tex_exports

        # ============================================================
        # 3. GET SHADER INFO FOR UNITY MATERIAL MAPPING
        # ============================================================
        shader_summary = {}
        for stage_name, stage_enum in [("fragment", rd.ShaderStage.Fragment), ("vertex", rd.ShaderStage.Vertex)]:
            sid = ps.GetShader(stage_enum)
            if not sid or int(sid) == 0:
                continue
            shader_summary[stage_name] = {"shaderId": int(sid)}
            entries = ctrl.GetShaderEntryPoints(sid)
            if entries:
                refl2 = ctrl.GetShader(pid, sid, entries[0])
                if refl2:
                    shader_summary[stage_name]["entryPoint"] = entries[0].name
                    shader_summary[stage_name]["inputCount"] = len(refl2.inputSignature)
                    shader_summary[stage_name]["outputCount"] = len(refl2.outputSignature)
                    shader_summary[stage_name]["textureCount"] = sum(1 for r in refl2.readOnlyResources if r.isTexture)
                    shader_summary[stage_name]["cbufferCount"] = len(refl2.constantBlocks)

                    # Get cbuffer values for material parameters
                    cbufs = []
                    try:
                        cbufs_bound = ps.GetConstantBuffers(stage_enum, False)
                    except Exception:
                        cbufs_bound = None

                    for idx, cb in enumerate(refl2.constantBlocks):
                        cb_info = {"name": getattr(cb, 'name', ''), "size": getattr(cb, 'byteSize', 0)}
                        if cbufs_bound and idx < len(cbufs_bound):
                            b = cbufs_bound[idx]
                            try:
                                vrs = ctrl.GetCBufferVariableContents(
                                    pid, sid, stage_enum, entries[0].name,
                                    idx, b.resource, getattr(b, 'byteOffset', 0), getattr(b, 'byteSize', 0))
                                cb_info["variables"] = [_var_dict(v) for v in vrs]
                            except Exception:
                                cb_info["variables"] = []
                        else:
                            try:
                                null_rid = rd.ResourceId()
                                vrs = ctrl.GetCBufferVariableContents(pid, sid, stage_enum, entries[0].name, idx, null_rid, 0, 0)
                                cb_info["variables"] = [_var_dict(v) for v in vrs]
                            except Exception:
                                cb_info["variables"] = []
                        cbufs.append(cb_info)
                    shader_summary[stage_name]["constantBuffers"] = cbufs

        result["shaderInfo"] = shader_summary

        # ============================================================
        # 4. GENERATE UNITY MATERIAL DEFINITION JSON
        # ============================================================
        unity_mat = {
            "materialName": mesh_name + "_Mat",
            "shader": "Custom/EyePBR",  # Default, user can change
            "renderPipeline": "auto-detect",
            "textures": {},
            "floats": {},
            "colors": {},
            "vectors": {},
        }

        # Map exported textures to Unity material properties
        _ROLE_TO_UNITY = {
            "albedo": {"property": "_MainTex", "urp": "_BaseMap", "hdrp": "_BaseColorMap"},
            "normal": {"property": "_BumpMap", "urp": "_BumpMap", "hdrp": "_NormalMap"},
            "metallic": {"property": "_MetallicGlossMap", "urp": "_MetallicGlossMap", "hdrp": "_MaskMap"},
            "roughness": {"property": "_SpecGlossMap", "urp": "_SpecGlossMap", "hdrp": "_MaskMap"},
            "ao": {"property": "_OcclusionMap", "urp": "_OcclusionMap", "hdrp": "_MaskMap"},
            "emissive": {"property": "_EmissionMap", "urp": "_EmissionMap", "hdrp": "_EmissiveColorMap"},
            "specular": {"property": "_SpecGlossMap", "urp": "_SpecGlossMap", "hdrp": "_SpecularColorMap"},
            "height": {"property": "_ParallaxMap", "urp": "_ParallaxMap", "hdrp": "_HeightMap"},
            "opacity": {"property": "_MainTex", "urp": "_BaseMap", "hdrp": "_BaseColorMap"},
            "environment": {"property": "_EnvCubeMap", "urp": "_ReflectionCubeMap", "hdrp": "_ReflectionCubeMap"},
            "shadow": {"property": "_ShadowMap", "urp": "_ShadowMap", "hdrp": "_ShadowMap"},
            "lightmap": {"property": "_LightMap", "urp": "_LightMap", "hdrp": "_LightMap"},
            "detail": {"property": "_DetailAlbedoMap", "urp": "_DetailAlbedoMap", "hdrp": "_DetailMap"},
            "unknown": {"property": "_Texture_%d", "urp": "_Texture_%d", "hdrp": "_Texture_%d"},
        }

        slot_counter = 0
        for tex in tex_exports:
            role = tex["role"]
            mapping = _ROLE_TO_UNITY.get(role, _ROLE_TO_UNITY["unknown"])
            prop_name = mapping["property"]
            if "%d" in prop_name:
                prop_name = prop_name % slot_counter
                slot_counter += 1

            unity_mat["textures"][prop_name] = {
                "resourceId": tex["resourceId"],
                "role": role,
                "originalSlot": tex["slot"],
                "shaderVar": tex["shaderVar"],
                "dimensions": "%dx%d" % (tex["width"], tex["height"]),
                "urpProperty": mapping["urp"] if "%d" not in mapping["urp"] else mapping["urp"] % (slot_counter - 1),
                "hdrpProperty": mapping["hdrp"] if "%d" not in mapping["hdrp"] else mapping["hdrp"] % (slot_counter - 1),
            }

        # Extract key material floats from cbuffer
        if "fragment" in shader_summary and "constantBuffers" in shader_summary["fragment"]:
            for cb in shader_summary["fragment"]["constantBuffers"]:
                for v in cb.get("variables", []):
                    name = v.get("name", "")
                    floats = v.get("float", [])
                    if not floats:
                        continue
                    name_lower = name.lower()
                    if "roughness" in name_lower or "smooth" in name_lower:
                        unity_mat["floats"]["_Smoothness"] = 1.0 - floats[0] if floats else 0.5
                        unity_mat["floats"]["_Roughness"] = floats[0] if floats else 0.5
                    elif "metallic" in name_lower or "metal" in name_lower:
                        unity_mat["floats"]["_Metallic"] = floats[0] if floats else 0.0
                    elif "color" in name_lower and len(floats) >= 3:
                        unity_mat["colors"]["_Color"] = {"r": floats[0], "g": floats[1], "b": floats[2], "a": floats[3] if len(floats) > 3 else 1.0}
                    elif "emissive" in name_lower and len(floats) >= 3:
                        unity_mat["colors"]["_EmissionColor"] = {"r": floats[0], "g": floats[1], "b": floats[2], "a": floats[3] if len(floats) > 3 else 1.0}

        # Save Unity material JSON
        mat_path = os.path.join(output_dir, mesh_name + "_material.json")
        with open(mat_path, "w") as f:
            json.dump(unity_mat, f, indent=2, ensure_ascii=False, default=str)
        result["materialFile"] = mat_path
        result["files"].append(mat_path)

        # ============================================================
        # 5. GENERATE UNITY IMPORT SCRIPT (C#)
        # ============================================================
        import_script = _generate_unity_import_script(mesh_name, unity_mat, tex_exports, output_dir)
        if import_script:
            result["importScript"] = import_script
            result["files"].append(import_script)

        # Save full summary
        summary_path = os.path.join(output_dir, mesh_name + "_unity_export.json")
        with open(summary_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        result["files"].append(summary_path)

        return result

    return _replay(_do)


def _extract_mesh_obj(ctrl, ps, mesh_out, mesh_name, output_dir, event_id=None):
    """Extract mesh data using VBuffer + IBuffer (Model Extractor approach).

    This uses the pipeline state's vertex inputs and buffers directly,
    which is more reliable than GetPostVSData for Vulkan/ANGLE captures.
    Falls back to PostVS if VBuffer approach fails.
    """
    import struct as _struct
    import math as _math

    # ============================================================
    # Method 1: VBuffer + IBuffer + VertexInputs (from Model Extractor)
    # ============================================================
    vbs = ps.GetVBuffers()
    ib = ps.GetIBuffer()
    attrs = ps.GetVertexInputs()

    # Get draw action info for index count
    actions = ctrl.GetRootActions()
    action = None
    cur_eid = int(mesh_out.numIndices) if mesh_out else 0  # Will use actual action

    def _find_action(actions_list, eid_target):
        for a in actions_list:
            if a.eventId == eid_target:
                return a
            if a.children:
                found = _find_action(a.children, eid_target)
                if found:
                    return found
        return None

    # Try to get the action from the current event
    try:
        # The event was already set by caller
        cur_event = ctrl.GetD3D11PipelineState()  # Just to trigger, won't use
    except Exception:
        pass

    def _read_vertex_attr(attr, vbs_list, indices_list, num_components=None):
        """Read vertex attribute data from vertex buffer (from Model Extractor)."""
        vb_idx = attr.vertexBuffer
        if vb_idx >= len(vbs_list):
            return []

        vb_info = vbs_list[vb_idx]
        if vb_info.resourceId == rd.ResourceId.Null():
            return []

        try:
            buf_data = ctrl.GetBufferData(vb_info.resourceId, 0, 0)
        except Exception:
            return []

        fmt = attr.format
        comp_count = num_components or fmt.compCount
        stride = vb_info.byteStride

        if stride == 0 or not buf_data:
            return []

        max_idx = max(indices_list) if indices_list else 0

        comp_map = {
            (rd.CompType.Float, 2): ('e', 2),    # float16
            (rd.CompType.Float, 4): ('f', 4),    # float32
            (rd.CompType.UInt, 1):  ('B', 1),
            (rd.CompType.UInt, 2):  ('H', 2),
            (rd.CompType.UInt, 4):  ('I', 4),
            (rd.CompType.SInt, 1):  ('b', 1),
            (rd.CompType.SInt, 2):  ('h', 2),
            (rd.CompType.SInt, 4):  ('i', 4),
            (rd.CompType.UNorm, 1): ('B', 1),
            (rd.CompType.UNorm, 2): ('H', 2),
            (rd.CompType.SNorm, 1): ('b', 1),
            (rd.CompType.SNorm, 2): ('h', 2),
        }

        key = (fmt.compType, fmt.compByteWidth)
        if key not in comp_map:
            if fmt.compByteWidth == 2:
                char, size = 'e', 2
            else:
                return []
        else:
            char, size = comp_map[key]

        unpack_fmt = '<%d%s' % (comp_count, char)
        is_unorm = fmt.compType == rd.CompType.UNorm
        is_snorm = fmt.compType == rd.CompType.SNorm
        unorm_max = float((2 ** (fmt.compByteWidth * 8)) - 1) if is_unorm else 1.0
        snorm_max = float(2 ** (fmt.compByteWidth * 8 - 1) - 1) if is_snorm else 1.0

        results = []
        for vi in range(max_idx + 1):
            offset = vb_info.byteOffset + attr.byteOffset + vi * stride
            if offset + comp_count * size > len(buf_data):
                results.append(tuple([0.0] * comp_count))
                continue
            try:
                raw = _struct.unpack_from(unpack_fmt, buf_data, offset)
                if is_unorm:
                    vals = tuple(v / unorm_max for v in raw)
                elif is_snorm:
                    vals = tuple(max(-1.0, v / snorm_max) for v in raw)
                else:
                    vals = tuple(float(v) for v in raw)
                cleaned = []
                for v in vals:
                    fv = float(v)
                    if not _math.isfinite(fv) or abs(fv) > 1e9:
                        cleaned.append(0.0)
                    else:
                        cleaned.append(fv)
                results.append(tuple(cleaned))
            except Exception:
                results.append(tuple([0.0] * comp_count))

        return results

    mesh_extracted = False
    positions = []
    normals = []
    uvs = []
    indices = []
    has_normals = False
    has_uvs = False

    if attrs and vbs:
        # Identify attributes (same logic as Model Extractor)
        pos_attr = None
        normal_attr = None
        uv_attr = None

        for attr in attrs:
            if getattr(attr, 'perInstance', False):
                continue
            name_lower = attr.name.lower()

            if pos_attr is None:
                if 'sv_position' not in name_lower and any(k in name_lower for k in ['position', 'pos']):
                    pos_attr = attr
            if normal_attr is None and any(k in name_lower for k in ['normal', 'norm']):
                normal_attr = attr
            if uv_attr is None and any(k in name_lower for k in ['texcoord', 'uv', 'tex']):
                uv_attr = attr

        # Heuristic fallback for position
        if pos_attr is None:
            for attr in attrs:
                if getattr(attr, 'perInstance', False):
                    continue
                name_lower = attr.name.lower()
                if any(k in name_lower for k in ['normal', 'texcoord', 'uv', 'color', 'tangent', 'sv_']):
                    continue
                if attr.format.compCount >= 3 and attr.format.compType == rd.CompType.Float:
                    pos_attr = attr
                    break

        if pos_attr is not None:
            # ---- Read indices (with indexOffset + baseVertex) ----
            # Find the current action to get baseVertex and indexOffset
            _cur_action = None
            def _find_cur(al):
                for a in al:
                    if hasattr(a, 'eventId') and a.eventId == int(event_id):
                        return a
                    if a.children:
                        f = _find_cur(a.children)
                        if f:
                            return f
                return None
            _cur_action = _find_cur(ctrl.GetRootActions())

            num_indices = mesh_out.numIndices if mesh_out else 0
            base_vertex = 0
            if _cur_action:
                base_vertex = getattr(_cur_action, 'baseVertex', getattr(_cur_action, 'vertexOffset', 0))
                num_indices = _cur_action.numIndices

            raw_indices = []
            is_indexed = ib.resourceId != rd.ResourceId.Null()
            if is_indexed:
                try:
                    ib_data = ctrl.GetBufferData(ib.resourceId, 0, 0)
                    # indexOffset: number of indices to skip in the index buffer
                    index_offset = getattr(_cur_action, 'indexOffset', 0) if _cur_action else 0
                    idx_offset_bytes = index_offset * ib.byteStride + ib.byteOffset
                    for i in range(num_indices):
                        off = idx_offset_bytes + i * ib.byteStride
                        if ib.byteStride == 2 and off + 2 <= len(ib_data):
                            raw_indices.append(_struct.unpack_from('<H', ib_data, off)[0])
                        elif ib.byteStride == 4 and off + 4 <= len(ib_data):
                            raw_indices.append(_struct.unpack_from('<I', ib_data, off)[0])
                except Exception:
                    raw_indices = list(range(num_indices))
                # Apply baseVertex offset
                if base_vertex != 0:
                    raw_indices = [idx + base_vertex for idx in raw_indices]
            else:
                raw_indices = list(range(base_vertex, base_vertex + num_indices))

            # ---- Remap indices to compact 0-based ----
            unique_verts = []
            old_to_new = {}
            for old_idx in raw_indices:
                if old_idx not in old_to_new:
                    old_to_new[old_idx] = len(unique_verts)
                    unique_verts.append(old_idx)
            indices = [old_to_new[idx] for idx in raw_indices]

            if indices and unique_verts:
                # ---- Read vertex attrs by unique_verts (precise) ----
                def _read_attr_precise(attr, wanted_comp):
                    """Read vertex data only for vertices in unique_verts."""
                    vb_idx = attr.vertexBuffer
                    if vb_idx >= len(vbs):
                        return []
                    vb_info = vbs[vb_idx]
                    if vb_info.resourceId == rd.ResourceId.Null():
                        return []
                    try:
                        buf_data = ctrl.GetBufferData(vb_info.resourceId, 0, 0)
                    except Exception:
                        return []
                    fmt = attr.format
                    orig_comp = fmt.compCount
                    stride = vb_info.byteStride
                    if stride == 0 or not buf_data:
                        return []

                    comp_map = {
                        (rd.CompType.Float, 2): ('e', 2),
                        (rd.CompType.Float, 4): ('f', 4),
                        (rd.CompType.UInt, 1):  ('B', 1),
                        (rd.CompType.UInt, 2):  ('H', 2),
                        (rd.CompType.UInt, 4):  ('I', 4),
                        (rd.CompType.SInt, 1):  ('b', 1),
                        (rd.CompType.SInt, 2):  ('h', 2),
                        (rd.CompType.SInt, 4):  ('i', 4),
                        (rd.CompType.UNorm, 1): ('B', 1),
                        (rd.CompType.UNorm, 2): ('H', 2),
                        (rd.CompType.SNorm, 1): ('b', 1),
                        (rd.CompType.SNorm, 2): ('h', 2),
                    }
                    key = (fmt.compType, fmt.compByteWidth)
                    if key not in comp_map:
                        if fmt.compByteWidth == 2:
                            char, sz = 'e', 2
                        else:
                            return []
                    else:
                        char, sz = comp_map[key]

                    # Always read original compCount to keep stride alignment correct
                    unpack_fmt = '<%d%s' % (orig_comp, char)
                    elem_size = orig_comp * sz
                    is_unorm = fmt.compType == rd.CompType.UNorm
                    is_snorm = fmt.compType == rd.CompType.SNorm
                    unorm_max = float((2 ** (fmt.compByteWidth * 8)) - 1) if is_unorm else 1.0
                    snorm_max = float(2 ** (fmt.compByteWidth * 8 - 1) - 1) if is_snorm else 1.0

                    results = []
                    for vi in unique_verts:
                        offset = vb_info.byteOffset + attr.byteOffset + vi * stride
                        if offset + elem_size > len(buf_data):
                            results.append(tuple([0.0] * wanted_comp))
                            continue
                        try:
                            raw = _struct.unpack_from(unpack_fmt, buf_data, offset)
                            if is_unorm:
                                vals = tuple(v / unorm_max for v in raw)
                            elif is_snorm:
                                vals = tuple(max(-1.0, v / snorm_max) for v in raw)
                            else:
                                vals = tuple(float(v) for v in raw)
                            # Truncate to wanted components, pad with 0 if not enough
                            cleaned = []
                            for ci in range(wanted_comp):
                                if ci < len(vals):
                                    fv = float(vals[ci])
                                    if not _math.isfinite(fv) or abs(fv) > 1e9:
                                        cleaned.append(0.0)
                                    else:
                                        cleaned.append(fv)
                                else:
                                    cleaned.append(0.0)
                            results.append(tuple(cleaned))
                        except Exception:
                            results.append(tuple([0.0] * wanted_comp))
                    return results

                # Read position (negate X for DX left-hand → right-hand coord system)
                raw_pos = _read_attr_precise(pos_attr, 3)
                positions = [(-p[0] if len(p) > 0 else 0.0, p[1] if len(p) > 1 else 0.0, p[2] if len(p) > 2 else 0.0) for p in raw_pos]

                # Read normals (with packed-encoding detection)
                if normal_attr:
                    raw_norm = _read_attr_precise(normal_attr, 3)
                    # Normalize + detect packed encoding
                    temp_normals = []
                    for n in raw_norm:
                        nx = n[0] if len(n) > 0 else 0.0
                        ny = n[1] if len(n) > 1 else 0.0
                        nz = n[2] if len(n) > 2 else 0.0
                        length = _math.sqrt(nx*nx + ny*ny + nz*nz)
                        if length > 1e-8:
                            nx, ny, nz = nx/length, ny/length, nz/length
                        else:
                            nx, ny, nz = 0.0, 0.0, 1.0
                        temp_normals.append((nx, ny, nz))
                    # Check if normals are valid (not packed/encoded data)
                    if temp_normals:
                        max_abs = max(max(abs(n[0]), abs(n[1]), abs(n[2])) for n in raw_norm)
                        if max_abs > 1.5:
                            # Packed encoding — discard, let importing tool recalculate
                            normals = []
                            has_normals = False
                        else:
                            normals = temp_normals
                            has_normals = len(normals) == len(positions)

                # Read UVs (NO flip — DX UV is already correct for Unity/FBX import)
                if uv_attr:
                    raw_uvs = _read_attr_precise(uv_attr, uv_attr.format.compCount)
                    for uv in raw_uvs:
                        u = uv[0] if len(uv) > 0 else 0.0
                        v = uv[1] if len(uv) > 1 else 0.0
                        uvs.append((u, v))
                    has_uvs = len(uvs) == len(positions)

                if positions:
                    mesh_extracted = True

    # ============================================================
    # Method 2: Fallback to PostVS (original approach)
    # ============================================================
    if not mesh_extracted and mesh_out:
        vb_rid = mesh_out.vertexResourceId
        vb_offset_val = mesh_out.vertexByteOffset
        vb_stride_val = mesh_out.vertexByteStride
        num_verts = mesh_out.numIndices

        if vb_rid and int(vb_rid) != 0 and vb_stride_val > 0 and num_verts > 0:
            try:
                vb_data = ctrl.GetBufferData(vb_rid, vb_offset_val, num_verts * vb_stride_val)
            except Exception:
                vb_data = None

            if vb_data and len(vb_data) >= vb_stride_val:
                # Detect layout from VS output signature
                pos_off = 0
                norm_off = -1
                uv_off = -1
                pid2 = _get_pipeline_obj(ps, ctrl)
                vs_id = ps.GetShader(rd.ShaderStage.Vertex)
                if vs_id and int(vs_id) != 0:
                    entries = ctrl.GetShaderEntryPoints(vs_id)
                    if entries:
                        refl2 = ctrl.GetShader(pid2, vs_id, entries[0])
                        if refl2 and refl2.outputSignature:
                            byte_off = 0
                            for sig in refl2.outputSignature:
                                sem = getattr(sig, 'semanticName', '').upper()
                                count = getattr(sig, 'compCount', 4)
                                sz = count * 4
                                if 'POSITION' in sem or 'SV_POSITION' in sem:
                                    pos_off = byte_off
                                elif 'NORMAL' in sem:
                                    norm_off = byte_off
                                elif 'TEXCOORD' in sem and uv_off < 0:
                                    uv_off = byte_off
                                byte_off += sz

                if vb_stride_val >= 32 and norm_off < 0:
                    norm_off = 16
                if vb_stride_val >= 40 and uv_off < 0:
                    uv_off = 32

                actual_verts = min(num_verts, len(vb_data) // vb_stride_val)
                for i in range(actual_verts):
                    base = i * vb_stride_val
                    if base + pos_off + 12 <= len(vb_data):
                        px, py, pz = _struct.unpack_from("<fff", vb_data, base + pos_off)
                        positions.append((px, py, pz))
                    if norm_off >= 0 and base + norm_off + 12 <= len(vb_data):
                        nx, ny, nz = _struct.unpack_from("<fff", vb_data, base + norm_off)
                        normals.append((nx, ny, nz))
                    if uv_off >= 0 and base + uv_off + 8 <= len(vb_data):
                        u, v = _struct.unpack_from("<ff", vb_data, base + uv_off)
                        uvs.append((u, v))

                has_normals = len(normals) == len(positions)
                has_uvs = len(uvs) == len(positions)

                # Use index buffer from PostVS
                if not indices:
                    ib_rid2 = mesh_out.indexResourceId
                    if ib_rid2 and int(ib_rid2) != 0:
                        ib_off2 = mesh_out.indexByteOffset
                        ib_stride2 = mesh_out.indexByteStride
                        try:
                            ib_data = ctrl.GetBufferData(ib_rid2, ib_off2, num_verts * ib_stride2)
                            for i in range(min(num_verts, len(ib_data) // ib_stride2)):
                                if ib_stride2 == 2:
                                    indices.append(_struct.unpack_from("<H", ib_data, i * 2)[0])
                                elif ib_stride2 == 4:
                                    indices.append(_struct.unpack_from("<I", ib_data, i * 4)[0])
                                else:
                                    indices.append(i)
                        except Exception:
                            indices = list(range(num_verts))
                    else:
                        indices = list(range(num_verts))

    if not positions:
        return None

    # ============================================================
    # Write OBJ file
    # ============================================================
    obj_path = os.path.join(output_dir, mesh_name + ".obj")
    num_faces = len(indices) // 3

    with open(obj_path, 'w', encoding='utf-8') as f:
        f.write("# Exported from RenderDoc MCP + Model Extractor\n")
        f.write("# Vertices: %d, Faces: %d\n\n" % (len(positions), num_faces))
        f.write("o %s\n\n" % mesh_name)

        for p in positions:
            f.write("v %.6f %.6f %.6f\n" % (p[0], p[1], p[2]))
        if uvs:
            f.write("\n")
            for uv in uvs:
                f.write("vt %.6f %.6f\n" % (uv[0], uv[1]))
        if normals:
            f.write("\n")
            for n in normals:
                f.write("vn %.6f %.6f %.6f\n" % (n[0], n[1], n[2]))
        f.write("\n")
        # Reverse winding order (swap i1/i2) to match X-axis flip for correct face normals
        for fi in range(num_faces):
            i0 = indices[fi * 3] + 1
            i1 = indices[fi * 3 + 1] + 1
            i2 = indices[fi * 3 + 2] + 1
            if has_uvs and has_normals:
                f.write("f %d/%d/%d %d/%d/%d %d/%d/%d\n" % (i0, i0, i0, i2, i2, i2, i1, i1, i1))
            elif has_uvs:
                f.write("f %d/%d %d/%d %d/%d\n" % (i0, i0, i2, i2, i1, i1))
            elif has_normals:
                f.write("f %d//%d %d//%d %d//%d\n" % (i0, i0, i2, i2, i1, i1))
            else:
                f.write("f %d %d %d\n" % (i0, i2, i1))

    # ============================================================
    # Write FBX file (ASCII 7.4, Unity compatible)
    # ============================================================
    fbx_path = os.path.join(output_dir, mesh_name + ".fbx")
    _write_fbx_ascii(fbx_path, mesh_name, positions, normals, uvs, indices, num_faces)

    return {
        "objPath": obj_path,
        "fbxPath": fbx_path,
        "vertexCount": len(positions),
        "triangleCount": num_faces,
        "hasNormals": has_normals,
        "hasUVs": has_uvs,
        "method": "VBuffer" if mesh_extracted else "PostVS",
    }


def _write_fbx_ascii(filepath, name, positions, normals, uvs, indices, num_faces):
    """Write FBX 7.4 ASCII file (from Model Extractor export_fbx)."""
    import math as _math

    num_verts = len(positions)
    has_normals = len(normals) == num_verts
    has_uvs = len(uvs) == num_verts

    if num_verts == 0 or num_faces == 0:
        return

    model_id = 1000000001
    geom_id = 1000000002
    material_id = 1000000003

    def safe_float(v):
        if not _math.isfinite(v) or abs(v) > 1e9:
            return 0.0
        return v

    flat_pos = []
    for p in positions:
        flat_pos.extend(safe_float(v) for v in p)

    # Reverse winding (swap i1/i2) to match X-axis flip
    fbx_idx = []
    for fi in range(num_faces):
        i0, i1, i2 = indices[fi * 3], indices[fi * 3 + 1], indices[fi * 3 + 2]
        fbx_idx.extend([i0, i2, -(i1 + 1)])

    # Edges
    edge_set = set()
    edges = []
    for fi in range(num_faces):
        base = fi * 3
        tri = [indices[fi * 3], indices[fi * 3 + 1], indices[fi * 3 + 2]]
        for j in range(3):
            v0, v1 = tri[j], tri[(j + 1) % 3]
            ek = (min(v0, v1), max(v0, v1))
            if ek not in edge_set:
                edge_set.add(ek)
                edges.append(base + j)

    def fmt_fa(arr, per=6):
        lines = []
        for i in range(0, len(arr), per):
            lines.append(",".join("%.6f" % v for v in arr[i:i + per]))
        return ",\n\t\t\t\t".join(lines)

    def fmt_ia(arr, per=12):
        lines = []
        for i in range(0, len(arr), per):
            lines.append(",".join(str(v) for v in arr[i:i + per]))
        return ",\n\t\t\t\t".join(lines)

    norm_sec = ""
    if has_normals:
        fn = []
        for fi in range(num_faces):
            for j in range(3):
                idx = indices[fi * 3 + j]
                if idx < len(normals):
                    fn.extend(safe_float(v) for v in normals[idx])
                else:
                    fn.extend([0.0, 0.0, 1.0])
        norm_sec = """
\t\tLayerElementNormal: 0 {
\t\t\tVersion: 102
\t\t\tName: "Normals"
\t\t\tMappingInformationType: "ByPolygonVertex"
\t\t\tReferenceInformationType: "Direct"
\t\t\tNormals: *%d {
\t\t\t\ta: %s
\t\t\t}
\t\t}""" % (len(fn), fmt_fa(fn))

    uv_sec = ""
    uv_idx_list = []
    if has_uvs:
        for fi in range(num_faces):
            for j in range(3):
                uv_idx_list.append(indices[fi * 3 + j])
        flat_uv = []
        for uv in uvs:
            flat_uv.extend(safe_float(v) for v in uv)
        uv_sec = """
\t\tLayerElementUV: 0 {
\t\t\tVersion: 101
\t\t\tName: "UVMap"
\t\t\tMappingInformationType: "ByPolygonVertex"
\t\t\tReferenceInformationType: "IndexToDirect"
\t\t\tUV: *%d {
\t\t\t\ta: %s
\t\t\t}
\t\t\tUVIndex: *%d {
\t\t\t\ta: %s
\t\t\t}
\t\t}""" % (len(flat_uv), fmt_fa(flat_uv), len(uv_idx_list), fmt_ia(uv_idx_list))

    mat_sec = """
\t\tLayerElementMaterial: 0 {
\t\t\tVersion: 101
\t\t\tName: ""
\t\t\tMappingInformationType: "AllSame"
\t\t\tReferenceInformationType: "IndexToDirect"
\t\t\tMaterials: *1 {
\t\t\t\ta: 0
\t\t\t}
\t\t}"""

    layer_entries = ""
    if has_normals:
        layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementNormal"
\t\t\t\tTypedIndex: 0
\t\t\t}"""
    if has_uvs:
        layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementUV"
\t\t\t\tTypedIndex: 0
\t\t\t}"""
    layer_entries += """
\t\t\tLayerElement:  {
\t\t\t\tType: "LayerElementMaterial"
\t\t\t\tTypedIndex: 0
\t\t\t}"""

    now = time.gmtime()

    content = """; FBX 7.4.0 project file
; Exported by RenderDoc MCP (Model Extractor engine)
FBXHeaderExtension:  {
\tFBXHeaderVersion: 1003
\tFBXVersion: 7400
\tCreationTimeStamp:  {
\t\tVersion: 1000
\t\tYear: %d
\t\tMonth: %d
\t\tDay: %d
\t\tHour: %d
\t\tMinute: %d
\t\tSecond: %d
\t\tMillisecond: 0
\t}
\tCreator: "RenderDoc MCP v1.2"
}

GlobalSettings:  {
\tVersion: 1000
\tProperties70:  {
\t\tP: "UpAxis", "int", "Integer", "",1
\t\tP: "UpAxisSign", "int", "Integer", "",1
\t\tP: "FrontAxis", "int", "Integer", "",2
\t\tP: "FrontAxisSign", "int", "Integer", "",1
\t\tP: "CoordAxis", "int", "Integer", "",0
\t\tP: "CoordAxisSign", "int", "Integer", "",1
\t\tP: "UnitScaleFactor", "double", "Number", "",1.0
\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1.0
\t}
}

Documents:  {
\tCount: 1
\tDocument: 100000000, "", "Scene" {
\t\tProperties70:  {
\t\t\tP: "SourceObject", "object", "", ""
\t\t\tP: "ActiveAnimStackName", "KString", "", "", ""
\t\t}
\t\tRootNode: 0
\t}
}

References:  {
}

Definitions:  {
\tVersion: 100
\tCount: 4
\tObjectType: "GlobalSettings" {
\t\tCount: 1
\t}
\tObjectType: "Model" {
\t\tCount: 1
\t}
\tObjectType: "Geometry" {
\t\tCount: 1
\t}
\tObjectType: "Material" {
\t\tCount: 1
\t}
}

Objects:  {
\tGeometry: %d, "Geometry::%s", "Mesh" {
\t\tVertices: *%d {
\t\t\ta: %s
\t\t}
\t\tPolygonVertexIndex: *%d {
\t\t\ta: %s
\t\t}
\t\tEdges: *%d {
\t\t\ta: %s
\t\t}
\t\tGeometryVersion: 124%s%s%s
\t\tLayer: 0 {
\t\t\tVersion: 100%s
\t\t}
\t}
\tModel: %d, "Model::%s", "Mesh" {
\t\tVersion: 232
\t\tProperties70:  {
\t\t\tP: "RotationActive", "bool", "", "",1
\t\t\tP: "InheritType", "enum", "", "",1
\t\t\tP: "DefaultAttributeIndex", "int", "Integer", "",0
\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A+",0,0,0
\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A+",0,0,0
\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A+",1,1,1
\t\t}
\t\tShading: T
\t\tCulling: "CullingOff"
\t}
\tMaterial: %d, "Material::DefaultMaterial", "" {
\t\tVersion: 102
\t\tShadingModel: "phong"
\t\tMultiLayer: 0
\t\tProperties70:  {
\t\t\tP: "DiffuseColor", "Color", "", "A",0.8,0.8,0.8
\t\t}
\t}
}

Connections:  {
\tC: "OO",%d,0
\tC: "OO",%d,%d
\tC: "OO",%d,%d
}
""" % (
        now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec,
        geom_id, name, num_verts * 3, fmt_fa(flat_pos),
        len(fbx_idx), fmt_ia(fbx_idx),
        len(edges), fmt_ia(edges),
        norm_sec, uv_sec, mat_sec, layer_entries,
        model_id, name,
        material_id,
        model_id, geom_id, model_id, material_id, model_id,
    )

    with open(filepath, 'w', encoding='ascii') as f:
        f.write(content)


# (Old PostVS-based mesh export code removed - replaced by VBuffer approach above)


def _generate_unity_import_script(mesh_name, unity_mat, tex_exports, output_dir):
    """Generate a C# editor script to auto-import the exported assets into Unity."""
    script_path = os.path.join(output_dir, mesh_name + "_UnityImport.cs")

    tex_assignments = []
    for prop_name, tex_data in unity_mat.get("textures", {}).items():
        tex_assignments.append(
            '            // Slot %s (%s): use save_texture(resource_id=%s) to export, then assign here'
            % (prop_name, tex_data.get("role", "unknown"), tex_data.get("resourceId", 0))
        )

    float_assignments = []
    for prop_name, val in unity_mat.get("floats", {}).items():
        float_assignments.append(
            '            mat.SetFloat("%s", %sf);' % (prop_name, val)
        )

    color_assignments = []
    for prop_name, col in unity_mat.get("colors", {}).items():
        color_assignments.append(
            '            mat.SetColor("%s", new Color(%sf, %sf, %sf, %sf));'
            % (prop_name, col["r"], col["g"], col["b"], col["a"])
        )

    script = '''// Auto-generated Unity Import Script from RenderDoc MCP
// Mesh: %(mesh_name)s | Event ID: exported from RenderDoc capture
//
// Usage: Place this script in Assets/Editor/ and run from menu:
//        RenderDoc > Import %(mesh_name)s

#if UNITY_EDITOR
using UnityEditor;
using UnityEngine;
using System.IO;

public class Import_%(safe_name)s
{
    [MenuItem("RenderDoc/Import %(mesh_name)s")]
    public static void Import()
    {
        string srcDir = EditorUtility.OpenFolderPanel("Select exported folder", "", "");
        if (string.IsNullOrEmpty(srcDir)) return;

        string destDir = "Assets/RenderDoc_Imports/%(mesh_name)s";
        if (!Directory.Exists(destDir))
            Directory.CreateDirectory(destDir);

        // Copy files to project
        foreach (string file in Directory.GetFiles(srcDir))
        {
            string ext = Path.GetExtension(file).ToLower();
            if (ext == ".obj" || ext == ".mtl" || ext == ".png" || ext == ".json")
            {
                string dest = Path.Combine(destDir, Path.GetFileName(file));
                File.Copy(file, dest, true);
            }
        }
        AssetDatabase.Refresh();

        // Create material
        string texDir = destDir;
        Material mat = new Material(Shader.Find("Standard"));
        mat.name = "%(mesh_name)s_Mat";

        // Assign textures
%(tex_assignments)s

        // Set float properties
%(float_assignments)s

        // Set color properties
%(color_assignments)s

        AssetDatabase.CreateAsset(mat, destDir + "/%(mesh_name)s_Mat.mat");

        // Try to assign material to imported mesh
        string objPath = destDir + "/%(mesh_name)s.obj";
        GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(objPath);
        if (prefab != null)
        {
            GameObject instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
            Renderer[] renderers = instance.GetComponentsInChildren<Renderer>();
            foreach (Renderer r in renderers)
            {
                r.sharedMaterial = mat;
            }
            Debug.Log("[RenderDoc MCP] Imported %(mesh_name)s with " + renderers.Length + " renderers");
        }

        AssetDatabase.SaveAssets();
        Debug.Log("[RenderDoc MCP] Import complete: " + destDir);
    }
}
#endif
''' % {
        "mesh_name": mesh_name,
        "safe_name": mesh_name.replace(" ", "_").replace("-", "_"),
        "tex_assignments": "\n".join(tex_assignments) if tex_assignments else "            // No textures to assign",
        "float_assignments": "\n".join(float_assignments) if float_assignments else "            // No float properties",
        "color_assignments": "\n".join(color_assignments) if color_assignments else "            // No color properties",
    }

    with open(script_path, "w") as f:
        f.write(script)

    return script_path


# ---- Method table ----
METHODS = {
    "ping": _ping,
    "get_capture_status": _get_capture_status,
    "get_frame_summary": _get_frame_summary,
    "get_draw_calls": _get_draw_calls,
    "get_draw_call_details": _get_draw_call_details,
    "get_pipeline_state": _get_pipeline_state,
    "get_shader_info": _get_shader_info,
    "get_textures": _get_textures,
    "get_buffers": _get_buffers,
    "get_resources": _get_resources,
    "get_texture_info": _get_texture_info,
    "get_texture_data": _get_texture_data,
    "get_buffer_data": _get_buffer_data,
    "pick_pixel": _pick_pixel,
    "get_texture_minmax": _get_texture_minmax,
    "pixel_history": _pixel_history,
    "debug_pixel": _debug_pixel,
    "debug_vertex": _debug_vertex,
    "enumerate_counters": _enumerate_counters,
    "fetch_counters": _fetch_counters,
    "get_post_vs_data": _get_post_vs_data,
    "get_debug_messages": _get_debug_messages,
    "find_by_texture": _find_by_texture,
    "find_by_shader": lambda p: {"error": "Not implemented yet"},
    "find_by_resource": lambda p: {"error": "Not implemented yet"},
    "save_texture": _save_texture,
    "list_captures": _list_captures,
    "open_capture": _open_capture,
    "get_bound_textures": _get_bound_textures,
    "reverse_shader": _reverse_shader,
    "export_drawcall": _export_drawcall,
    "export_to_unity": _export_to_unity,
    "debug_vulkan_bindings": _debug_vulkan_bindings,
    "identify_drawcalls": _identify_drawcalls,
    "analyze_lighting": _analyze_lighting,
}


# =====================================================================
# Extension entry points
# =====================================================================

def register(version, ctx):
    global _ctx, _poller
    _ctx = ctx

    _poller = JsonPoller(handle_request) if HAS_UI else None
    if _poller:
        _poller.start()

    if HAS_UI:
        try:
            ctx.Extensions().RegisterWindowMenu(
                qrd.WindowMenu.Tools, ["RenderDoc MCP", "Status"], _show_status)
        except Exception as e:
            print("[RD-MCP] Menu error: %s" % str(e))

    print("[RD-MCP] Loaded (RenderDoc %s)" % version)
    print("[RD-MCP] IPC: %s" % IPC_DIR)


def unregister():
    global _poller
    if _poller:
        _poller.stop()
        _poller = None
    print("[RD-MCP] Unloaded")


def _show_status(ctx, data):
    if _poller:
        msg = "MCP bridge is RUNNING\nIPC: %s\nMethods: %s" % (IPC_DIR, ", ".join(sorted(METHODS.keys())))
        ctx.Extensions().MessageDialog(msg, "RenderDoc MCP")
    else:
        ctx.Extensions().ErrorDialog("MCP bridge is NOT running", "RenderDoc MCP")
