# -*- coding: utf-8 -*-
"""
Derive lighting + camera parameters from the capture.

Run:
    qTinecmaTool.exe --python E:\\GST\\libai_scene\\probe_light_cam.py

Two targets:
  * CAMERA: the projection matrix in the VS constant blocks gives us the
    vertical FOV and near/far. The showcase parts all sit near (0,0,106) in
    camera space, so a Blender camera at the origin looking down -Z with that
    FOV reproduces the original framing.
  * SUN: the 1024x1024 shadow passes carry a light-space view matrix. Its
    third row/column is the light forward direction; we recover it and convert
    to a Blender sun rotation.
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import json
import math
import struct
import traceback

import renderdoc as rd

RDC_PATH = r"E:\GST\libai_scene\libai.rdc"
OUT_DIR = r"E:\GST\libai_scene\out"

# main-pass draw used to read the camera projection
CAM_EID = 260
# shadow-pass draws (1024x1024, no textures) used to read the light matrix
SHADOW_EIDS = [170, 203, 186, 192, 209, 182, 160, 176, 166, 197]


def all_cb_matrices(controller, eid):
    """Return {varname: 16-float tuple} for every 64-byte window in VS CBs."""
    out = {}
    try:
        controller.SetFrameEvent(eid, True)
        state = controller.GetPipelineState()
        refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if not refl:
            return out
        runtime = state.GetConstantBlocks(rd.ShaderStage.Vertex)
        for i, cbr in enumerate(refl.constantBlocks):
            rcb = runtime[i] if i < len(runtime) else None
            if rcb is None:
                continue
            try:
                d = rcb.descriptor
                buf = controller.GetBufferData(d.resource,
                                               getattr(d, "byteOffset", 0),
                                               getattr(d, "byteSize", cbr.byteSize))
            except Exception:
                continue
            for var in cbr.variables:
                try:
                    nm = str(var.name)
                    off = getattr(var, "byteOffset", getattr(var, "offset", 0))
                except Exception:
                    continue
                if len(buf) < off + 64:
                    continue
                try:
                    out["cb{0}.{1}".format(i, nm)] = struct.unpack_from('16f', buf, off)
                except Exception:
                    continue
    except Exception:
        pass
    return out


def looks_projection(m):
    """Perspective projection: m[11] == -1 (or +1) and m[15] == 0.

    Stored column-major here, so the -1 lands at index 11 and the 4th
    diagonal entry is zero.
    """
    return abs(m[15]) < 1e-6 and abs(abs(m[11]) - 1.0) < 1e-3


def fov_from_proj(m):
    """Vertical FOV (deg), aspect and near/far from a perspective matrix."""
    # column-major: m[5] = 1/tan(fovy/2)  (times aspect handling in m[0])
    f_y = m[5]
    f_x = m[0]
    if abs(f_y) < 1e-9:
        return None
    fovy = 2.0 * math.atan(1.0 / abs(f_y))
    fovx = 2.0 * math.atan(1.0 / abs(f_x)) if abs(f_x) > 1e-9 else None
    aspect = (abs(f_y) / abs(f_x)) if abs(f_x) > 1e-9 else None
    # near/far from m[10], m[14]: standard GL/VK style
    A, B = m[10], m[14]
    near = far = None
    try:
        if abs(A + 1.0) > 1e-9:
            near = B / (A - 1.0)
            far = B / (A + 1.0)
    except Exception:
        pass
    return {
        "fovY_deg": math.degrees(fovy),
        "fovX_deg": math.degrees(fovx) if fovx else None,
        "aspect": aspect,
        "near": near, "far": far,
        "m10": A, "m14": B,
    }


def main():
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except Exception:
        pass
    lines = []

    def log(m):
        print(m)
        lines.append(str(m))

    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(RDC_PATH, '', None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenFile")
        return
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenCapture")
        cap.Shutdown()
        return

    result = {}
    try:
        # ---------------- camera ----------------
        log("=" * 60)
        log("CAMERA  (from EID {0} vertex CBs)".format(CAM_EID))
        log("=" * 60)
        mats = all_cb_matrices(controller, CAM_EID)
        cams = []
        for nm, m in sorted(mats.items()):
            if not looks_projection(m):
                continue
            info = fov_from_proj(m)
            if not info:
                continue
            info["var"] = nm
            info["matrix"] = [float(x) for x in m]
            cams.append(info)
            log("  {0}".format(nm))
            log("     fovY={0:.2f}deg fovX={1}  aspect={2}".format(
                info["fovY_deg"],
                "{0:.2f}".format(info["fovX_deg"]) if info["fovX_deg"] else "?",
                "{0:.4f}".format(info["aspect"]) if info["aspect"] else "?"))
            log("     near={0}  far={1}".format(info["near"], info["far"]))
        if not cams:
            log("  no perspective projection matrix found; dumping all vars:")
            for nm, m in sorted(mats.items()):
                log("    {0}: [{1}]".format(nm, ", ".join("{0:.3f}".format(x) for x in m)))
        result["cameras"] = cams

        # ---------------- sun / shadow ----------------
        log("")
        log("=" * 60)
        log("SHADOW / SUN  (from {0} shadow passes)".format(len(SHADOW_EIDS)))
        log("=" * 60)
        suns = []
        for eid in SHADOW_EIDS:
            mats = all_cb_matrices(controller, eid)
            for nm, m in sorted(mats.items()):
                # light view-projection: often orthographic -> m[15]==1, m[11]==0
                # We want the rotation part's 3rd basis vector = light forward.
                if abs(m[15] - 1.0) > 1e-6:
                    continue
                # column-major rotation columns
                fwd = (m[2], m[6], m[10])
                ln = math.sqrt(sum(c * c for c in fwd))
                if ln < 1e-6:
                    continue
                fwd = tuple(c / ln for c in fwd)
                # reject non-orthonormal (skip projection-ish blocks)
                r0 = (m[0], m[4], m[8])
                l0 = math.sqrt(sum(c * c for c in r0))
                if l0 < 1e-6 or abs(l0 - ln) / ln > 0.35:
                    continue
                trans = (m[12], m[13], m[14])
                suns.append({"eid": eid, "var": nm, "forward": fwd,
                             "scale": ln, "trans": trans,
                             "matrix": [float(x) for x in m]})
        # group identical directions -> the real sun is shared across passes
        groups = {}
        for s in suns:
            key = tuple(round(c, 3) for c in s["forward"])
            groups.setdefault(key, []).append(s)
        log("  {0} candidate light matrices, {1} distinct direction(s)".format(
            len(suns), len(groups)))
        for key, lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            eids = sorted(set(x["eid"] for x in lst))
            log("   dir=({0:6.3f},{1:6.3f},{2:6.3f})  seen in {3} matrices, EIDs={4}".format(
                key[0], key[1], key[2], len(lst), eids[:8]))
            log("        vars: {0}".format(", ".join(sorted(set(x["var"] for x in lst))[:4])))
        result["sunCandidates"] = [
            {"forward": list(k), "count": len(v),
             "eids": sorted(set(x["eid"] for x in v)),
             "vars": sorted(set(x["var"] for x in v))}
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ]

        with open(os.path.join(OUT_DIR, "lightcam.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        log("")
        log("wrote lightcam.json")
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
        try:
            with open(os.path.join(OUT_DIR, "lightcam_log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass


if __name__ == "__main__":
    main()
    sys.exit(0)
