# -*- coding: utf-8 -*-
"""
Scene exporter for the 李白谪仙 character-showcase capture.

Run:
    qTinecmaTool.exe --python E:\\GST\\libai_scene\\export_scene.py

What it does, per selected draw call:
  * reads the vertex streams and index buffer
  * maps attributes with an EXPLICIT layout rule (see infer_mapper_explicit)
  * applies the per-draw `_child1` model-view matrix so every part lands in the
    same showcase space the original frame had
  * writes a multi-UV ASCII FBX + all bound fragment textures
  * records material/binding metadata to scene.json for the Blender rebuild

Reuses the FBX writer + unpack helpers from batch_fbx_exporter_ExtraUV.
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import json
import struct
import shutil
import traceback
from collections import defaultdict

import renderdoc as rd

# Pull in the proven FBX writer / unpack helpers from the batch FBX exporter
# extension that ships in the build tree.
#
# NOTE: this used to point at a standalone `headless_carbody_export.py` under
# extensions/batch_fbx_exporter_ExtraUV, which no longer exists. The helpers now
# live in the extension package itself. It must be imported as a PACKAGE (its
# __init__ does `from .batch_dialog import ...`), so the PLUGINS dir goes on
# sys.path -- not the package dir. The package also pulls in PySide2 +
# qrenderdoc, which is fine because we run inside the qrenderdoc GUI process.
PLUGINS_DIR = r"E:\GraphicsDebugger\x64\Development\Plugins"
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)

import batch_fbx_exporter_ExtraUV as HX  # noqa: E402


def _find_action(controller, eid):
    """Locate the action/draw with this exact eventId.

    The exporter package only offers find_draws_in_range(); this is the
    single-eid equivalent, walking the action tree the same way.
    """
    found = []

    def walk(act):
        if found:
            return
        if act.eventId == eid:
            found.append(act)
            return
        for child in act.children:
            walk(child)
            if found:
                return

    for root in controller.GetRootActions():
        walk(root)
        if found:
            break
    return found[0] if found else None


# ==================== CONFIG ====================

RDC_PATH = r"E:\GST\libai_scene\libai.rdc"
OUT_ROOT = r"E:\GST\libai_scene\scene_export"

# Draw calls to export, grouped for tidy output folders.
# Derived from out/survey.txt. The frame has 121 draws total; the ones left out
# are the 1024x1024 shadow pass (eid 160-209, duplicates of the hero parts),
# the bloom down/up-sample chain (eid 779-977, viewports shrinking 480->30),
# the swapchain blits (eid 16-124, 1002) and the sub-24-index UI bits.
#
# Groups 05-07 were added in the "scene background" expansion pass. Selection
# rule for those: viewport 1920x1080, hasDepth, numAttrs >= 3, numIndices >= 24
# and not already covered above -> 50 draws, 15567 indices.
GROUPS = {
    "01_hero": [260, 279, 562, 692, 274, 253, 246],
    "02_hero_extraUV": [647, 267, 569],
    "03_props": [365, 353, 334, 322, 338, 377, 407, 389, 423, 307],
    "04_bg_board": [699, 429, 456, 601, 719],

    # Drawn BEFORE the hero (eid 231/238) -> sky dome / backdrop plate, plus
    # the large 1710-index board at 449.
    "05_backdrop": [231, 238, 449],

    # Same texture sets as the group 03/04 FX, i.e. ribbons and firefly cards
    # the first export pass simply missed:
    #   391/411 share (150547, 151089) with the 03_props ribbons
    #   596/619 share (150696, 151094) with the 601/719 firefly cards
    "06_fx_extra": [391, 411, 596, 619],

    # Remaining scene geometry: rock detail, foliage, small set dressing.
    "07_scene_detail": [
        313, 344, 397, 398,                     # tex 151182 family
        463, 470, 477, 484, 491, 498, 505,      # tex 149950 / 149627 families
        512, 516, 523, 530, 537, 544, 548,
        555, 576, 583, 589, 608, 612, 626, 633,
        640, 654, 655, 656, 663, 670, 677, 685,
        712, 726, 730, 732, 736, 740, 744,
        750, 754, 757,
    ],
}

# Draws whose positions are ALREADY in showcase/world space (probe_verts showed
# z ~ 125 with large extents) -> do not apply the model-view matrix.
# The 06_fx_extra ribbons belong to the same families as the 03_props ones, so
# they get the same treatment.
NO_TRANSFORM_EIDS = set([365, 353, 334, 322, 338, 377, 407, 389, 423, 307,
                         391, 411])

# Name of the CB variable holding the per-draw model-view matrix.
MV_VAR_CANDIDATES = ["_child1", "_child0", "_child3"]

EXPORT_TEXTURES = True


# ==================== explicit attribute mapping ====================

def infer_mapper_explicit(meshInputs, log):
    """Map attributes for THIS capture's layouts.

    Observed families (from out/survey.txt), all components float unless noted:
        A) _input0:3F _input1:3F _input2:4F _input3:2F _input4:2F  [+_input5:2F/3F]
           -> pos, normal, tangent(4), uv, uv2 [, uv3]
        B) _input0:3F _input1:4UNorm _input2:2F [_input3:2F]
           -> pos, color, uv [, uv2]
        C) _input0:3F _input1:3F _input2:4UNorm _input3:2F
           -> pos, normal, color, uv
        D) _input0:3F _input1:4F _input2:2F [_input3:2F]
           -> pos, tangent(4), uv [, uv2]

    Rule set (positional, not the generic float-is-always-UV heuristic which
    would swallow the normal/tangent streams in family A):
      * first float attr with >=3 comps        -> POSITION
      * a 3-comp float attr right after pos    -> NORMAL
      * a 4-comp FLOAT attr                    -> TANGENT (xyz + handedness w)
      * a 4-comp UNorm attr                    -> COLOR
      * every 2-comp float attr, in order      -> UV, UV2, UV3...
      * a trailing 3-comp float (after UVs)    -> UV3 (.xy)
    """
    attrs = []
    for mi in meshInputs:
        fmt = mi.format
        if fmt.Special():
            continue
        attrs.append({
            "name": str(mi.name),
            "compCount": int(fmt.compCount),
            "compType": str(fmt.compType).replace("CompType.", ""),
            "byteWidth": int(fmt.compByteWidth),
            "isFloat": fmt.compType == rd.CompType.Float,
        })

    mapper = {k: "" for k in ["POSITION", "NORMAL", "BINORMAL", "TANGENT",
                              "COLOR", "UV", "UV2", "UV3", "UV4", "UV5"]}
    used = set()

    # POSITION: first float attr with >= 3 components
    for a in attrs:
        if a["isFloat"] and a["compCount"] >= 3:
            mapper["POSITION"] = a["name"]
            used.add(a["name"])
            break

    # NORMAL: next 3-component float
    for a in attrs:
        if a["name"] in used:
            continue
        if a["isFloat"] and a["compCount"] == 3:
            mapper["NORMAL"] = a["name"]
            used.add(a["name"])
            break

    # TANGENT: 4-component float (xyz direction + w handedness)
    for a in attrs:
        if a["name"] in used:
            continue
        if a["isFloat"] and a["compCount"] == 4:
            mapper["TANGENT"] = a["name"]
            used.add(a["name"])
            break

    # COLOR: 4-component UNorm / single-byte
    for a in attrs:
        if a["name"] in used:
            continue
        if a["compCount"] == 4 and ("UNorm" in a["compType"] or a["byteWidth"] == 1):
            mapper["COLOR"] = a["name"]
            used.add(a["name"])
            break

    # UV sets: every remaining 2-component float, in layout order
    uv_slots = ["UV", "UV2", "UV3", "UV4", "UV5"]
    ui = 0
    for a in attrs:
        if a["name"] in used or not a["isFloat"]:
            continue
        if a["compCount"] == 2 and ui < len(uv_slots):
            mapper[uv_slots[ui]] = a["name"]
            used.add(a["name"])
            ui += 1

    # trailing 3-comp float after the UVs -> one more UV set (.xy)
    for a in attrs:
        if a["name"] in used or not a["isFloat"]:
            continue
        if a["compCount"] == 3 and ui < len(uv_slots):
            mapper[uv_slots[ui]] = a["name"] + ".xy"
            used.add(a["name"])
            ui += 1

    log("    layout: " + " | ".join(
        "{0}:{1}x{2}".format(a["name"], a["compCount"], a["compType"]) for a in attrs))
    log("    mapped: " + " ".join(
        "{0}={1}".format(k, mapper[k]) for k in
        ["POSITION", "NORMAL", "TANGENT", "COLOR", "UV", "UV2", "UV3"] if mapper[k]))
    return mapper, attrs


# ==================== matrix ====================

def get_modelview(controller, log):
    """Read the per-draw model-view matrix from the VS constant blocks.

    Returns a ROW-major 16-tuple suitable for HX.transform_vertices_with_matrix
    (which indexes m[0..3] as the first row including translation in m[3]).

    The capture stores it COLUMN-major (translation at m[12..14]), so we
    transpose on the way out.
    """
    try:
        state = controller.GetPipelineState()
        refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if not refl:
            return None, None
        runtime_cbs = state.GetConstantBlocks(rd.ShaderStage.Vertex)

        for cb_index, cb_refl in enumerate(refl.constantBlocks):
            runtime_cb = runtime_cbs[cb_index] if cb_index < len(runtime_cbs) else None
            if runtime_cb is None:
                continue
            try:
                desc = runtime_cb.descriptor
                buf = controller.GetBufferData(desc.resource,
                                               getattr(desc, "byteOffset", 0),
                                               getattr(desc, "byteSize", cb_refl.byteSize))
            except Exception:
                continue

            byname = {}
            for var in cb_refl.variables:
                try:
                    byname[str(var.name)] = getattr(var, "byteOffset",
                                                    getattr(var, "offset", 0))
                except Exception:
                    continue

            for cand in MV_VAR_CANDIDATES:
                if cand not in byname:
                    continue
                off = byname[cand]
                if len(buf) < off + 64:
                    continue
                try:
                    m = struct.unpack_from('16f', buf, off)
                except Exception:
                    continue
                # column-major affine check: last row is (0,0,0,1)
                if not (abs(m[12]) < 1e-6 and abs(m[13]) < 1e-6 and
                        abs(m[14]) < 1e-6 and abs(m[15] - 1.0) < 1e-6):
                    # already row-major affine? translation in m[3],m[7],m[11]
                    if (abs(m[3]) < 1e-6 and abs(m[7]) < 1e-6 and
                            abs(m[11]) < 1e-6 and abs(m[15] - 1.0) < 1e-6):
                        # rows are (m0..m3),(m4..m7)... translation already in col 4
                        # but our captures put translation in row 4 -> transpose it
                        rowmajor = (m[0], m[4], m[8], m[12],
                                    m[1], m[5], m[9], m[13],
                                    m[2], m[6], m[10], m[14],
                                    0.0, 0.0, 0.0, 1.0)
                        return rowmajor, cand
                    continue
                # transpose column-major -> row-major with translation in col 4
                rowmajor = (m[0], m[4], m[8], m[12],
                            m[1], m[5], m[9], m[13],
                            m[2], m[6], m[10], m[14],
                            0.0, 0.0, 0.0, 1.0)
                return rowmajor, cand
        return None, None
    except Exception as e:
        log("    matrix error: {0}".format(e))
        return None, None


# ==================== textures ====================

def save_textures_detailed(controller, tex_dir, log):
    """Export bound fragment textures, returning metadata per slot."""
    try:
        os.makedirs(tex_dir, exist_ok=True)
    except Exception:
        pass
    state = controller.GetPipelineState()
    out = []
    seen = set()
    try:
        used = state.GetReadOnlyResources(rd.ShaderStage.Fragment)
    except Exception as e:
        log("    tex enumerate failed: {0}".format(e))
        return out

    for slot, ud in enumerate(used):
        try:
            res = ud.descriptor.resource
        except Exception:
            continue
        if res == rd.ResourceId.Null():
            continue
        rid = int(res)
        entry = {"slot": slot, "id": rid}
        try:
            t = controller.GetTexture(res)
            if t is not None:
                entry["w"] = int(t.width)
                entry["h"] = int(t.height)
                entry["mips"] = int(t.mips)
                entry["fmt"] = str(t.format.Name())
                entry["cubemap"] = bool(getattr(t, "cubemap", False))
        except Exception:
            pass

        fname = "tex_{0}.png".format(rid)
        path = os.path.join(tex_dir, fname)
        if rid not in seen:
            seen.add(rid)
            if not os.path.isfile(path):
                ts = rd.TextureSave()
                ts.resourceId = res
                ts.mip = 0
                ts.slice.sliceIndex = 0
                ts.alpha = rd.AlphaMapping.Preserve
                ts.destType = rd.FileType.PNG
                try:
                    controller.SaveTexture(ts, path)
                except Exception as e:
                    entry["error"] = str(e)
        entry["file"] = fname
        out.append(entry)
    return out


# ==================== per-draw export ====================

def export_draw(controller, eid, out_dir, log):
    draw = _find_action(controller, eid)
    if draw is None:
        log("  EID {0}: not a draw / not found".format(eid))
        return None

    controller.SetFrameEvent(eid, True)
    meshInputs = HX.getMeshInputs(controller, draw)
    if not meshInputs:
        log("  EID {0}: no mesh inputs".format(eid))
        return None

    log("  EID {0}: indices={1}".format(eid, draw.numIndices))
    mapper, attr_meta = infer_mapper_explicit(meshInputs, log)

    indices = HX.getIndices(controller, meshInputs[0])
    if not indices:
        log("    no indices")
        return None

    data = defaultdict(list)
    attr_list = set()
    max_idx = max(indices)
    for attr in meshInputs:
        if attr.format.Special():
            continue
        attr_list.add(attr.name)
        size = (max_idx + 1) * attr.vertexByteStride
        try:
            full = controller.GetBufferData(attr.vertexResourceId,
                                            attr.vertexByteOffset, size)
            for idx in indices:
                off = idx * attr.vertexByteStride
                data[attr.name].append(HX.unpackData(attr.format, full[off:]))
        except Exception as e:
            log("    attr {0} failed: {1}".format(attr.name, e))
    data["IDX"] = indices

    meta = {
        "eventId": eid,
        "numIndices": int(draw.numIndices),
        "uniqueVerts": len(set(indices)),
        "mapper": mapper,
        "attributes": attr_meta,
    }

    # --- transform into showcase space ---
    if eid in NO_TRANSFORM_EIDS:
        log("    transform: skipped (already showcase space)")
        meta["transform"] = None
    else:
        m, var = get_modelview(controller, log)
        if m:
            data = HX.transform_vertices_with_matrix(data, m, mapper)
            meta["transform"] = {"var": var, "matrix": [float(x) for x in m]}
            log("    transform: applied from '{0}' trans=({1:.2f},{2:.2f},{3:.2f})".format(
                var, m[3], m[7], m[11]))
        else:
            meta["transform"] = None
            log("    transform: no matrix found -> object space")

    # --- textures ---
    if EXPORT_TEXTURES:
        texs = save_textures_detailed(controller, os.path.join(out_dir, "textures"), log)
        meta["textures"] = texs
        log("    textures: {0}".format(len(texs)))

    # --- FBX ---
    fbx_name = "eid{0}.fbx".format(eid)
    fbx_path = os.path.join(out_dir, fbx_name)
    # NOTE: export_fbx takes a trailing `controller` arg that its body never
    # actually touches (verified: the name appears only in the signature), so
    # passing the real controller is harmless and keeps the call honest.
    if HX.export_fbx(fbx_path, mapper, data, attr_list, controller):
        meta["fbx"] = fbx_name
        uvsets = [k for k in ["UV", "UV2", "UV3", "UV4", "UV5"] if mapper.get(k)]
        log("    FBX -> {0}  ({1} UV set(s))".format(fbx_name, len(uvsets)))
        return meta
    log("    FBX export FAILED")
    return None


def main():
    if os.path.isdir(OUT_ROOT):
        pass
    try:
        os.makedirs(OUT_ROOT, exist_ok=True)
    except Exception:
        pass

    lines = []

    def flush():
        try:
            with open(os.path.join(OUT_ROOT, "export_log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass

    # NOTE: qTinecmaTool swallows stdout and the sandbox can hard-kill the
    # process tree, so the log must hit disk on every line -- otherwise a hang
    # is indistinguishable from a silent crash. Flushing 500 short lines costs
    # nothing next to the replay work.
    def log(m):
        print(m)
        lines.append(str(m))
        flush()

    log("scene export: {0}".format(RDC_PATH))
    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(RDC_PATH, '', None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenFile {0}".format(st.code))
        flush()
        return
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenCapture {0}".format(st.code))
        cap.Shutdown()
        flush()
        return

    scene = {"rdc": RDC_PATH, "groups": {}}
    try:
        for group, eids in sorted(GROUPS.items()):
            gdir = os.path.join(OUT_ROOT, group)
            try:
                os.makedirs(gdir, exist_ok=True)
            except Exception:
                pass
            log("")
            log("=" * 62)
            log("group {0}  ({1} draws)".format(group, len(eids)))
            log("=" * 62)
            items = []
            for eid in eids:
                try:
                    meta = export_draw(controller, eid, gdir, log)
                    if meta:
                        items.append(meta)
                except Exception as e:
                    log("  EID {0} FATAL: {1}".format(eid, e))
                    log(traceback.format_exc())
            scene["groups"][group] = items
            log("group {0}: {1}/{2} exported".format(group, len(items), len(eids)))

        with open(os.path.join(OUT_ROOT, "scene.json"), "w", encoding="utf-8") as f:
            json.dump(scene, f, ensure_ascii=False, indent=1)
        total = sum(len(v) for v in scene["groups"].values())
        log("")
        log("TOTAL exported: {0}".format(total))
        log("scene.json written")
    except Exception as e:
        log("FATAL {0}".format(e))
        log(traceback.format_exc())
    finally:
        try:
            controller.Shutdown()
        except Exception:
            pass
        try:
            cap.Shutdown()
        except Exception:
            pass
        flush()


if __name__ == "__main__":
    main()
    sys.exit(0)
