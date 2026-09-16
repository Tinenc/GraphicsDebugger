# -*- coding: utf-8 -*-
"""
Export the capture's final framebuffer as a reference image, and verify the
handedness / orientation of the space our exported vertices live in.

Run with:  qTinecmaTool.exe --python export_reference.py

Why this matters: the projection matrix recovered from EID 260 is
    col0 = (2.101, 0, 0, 0)
    col1 = (0, 3.736, 0, 0)
    col2 = (0, 0, 1.000, 1.000)
    col3 = (-0.006, -0.949, -100.171, -100.061)
so  w_clip = z_view - 100.061  ->  the eye sits at z = +100.061 and looks
towards +Z, and  y_ndc = (3.736*y - 0.949) / w.  Vulkan NDC is Y-down, so a
positive view-space Y projects to the LOWER half of the image.  We confirm
that numerically here by projecting each exported draw's bounding box and
reporting where it lands in pixels - then it can be checked against the
reference PNG by eye.
"""

import json
import os
import sys

import renderdoc as rd

RDC = r"E:\GST\libai_scene\libai.rdc"
OUT = r"E:\GST\libai_scene\out"
REF_DIR = r"E:\GST\libai_scene\scene_export\_reference"

# projection recovered from EID 260 (column-major, as read from the CB)
PROJ_M00 = 2.101
PROJ_M11 = 3.736
PROJ_TX = -0.006
PROJ_TY = -0.949
EYE_Z = 100.061

# the draws we actually exported, with the group they went into
GROUPS = {
    "01_hero": [260, 279, 562, 692, 274, 253, 246],
    "02_hero_extraUV": [647, 267, 569],
    "03_props": [365, 353, 334, 322, 338, 377, 407, 389, 423, 307],
    "04_bg_board": [699, 429, 456, 601, 719],
}

LOG = []


def log(msg):
    LOG.append(str(msg))
    print(msg)


def save_texture(controller, resid, path, name):
    """Write one resource out as PNG at its native size."""
    if resid == rd.ResourceId.Null():
        return False
    texsave = rd.TextureSave()
    texsave.resourceId = resid
    texsave.mip = 0
    texsave.slice.sliceIndex = 0
    texsave.alpha = rd.AlphaMapping.Discard
    texsave.destType = rd.FileType.PNG
    try:
        controller.SaveTexture(texsave, path)
        log("  saved {0} -> {1}".format(name, os.path.basename(path)))
        return True
    except Exception as e:
        log("  FAILED {0}: {1}".format(name, e))
        return False


def project(x, y, z):
    """view-space point -> (px, py) in a 1920x1080 Vulkan framebuffer."""
    w = z - EYE_Z
    if abs(w) < 1e-6:
        return None
    xn = (PROJ_M00 * x + PROJ_TX) / w
    yn = (PROJ_M11 * y + PROJ_TY) / w
    # Vulkan NDC: x in [-1,1] left->right, y in [-1,1] TOP->BOTTOM
    px = (xn + 1.0) * 0.5 * 1920.0
    py = (yn + 1.0) * 0.5 * 1080.0
    return px, py


def main(controller):
    if not os.path.isdir(REF_DIR):
        os.makedirs(REF_DIR)

    # ---------------- 1. final framebuffer ----------------
    log("=" * 60)
    log("FINAL FRAMEBUFFER")
    log("=" * 60)

    # walk to the very last action so we grab the presented image
    actions = controller.GetRootActions()

    def last_leaf(acts):
        best = None
        for a in acts:
            if a.children:
                c = last_leaf(a.children)
                if c:
                    best = c
            elif a.numIndices > 0 or a.flags & rd.ActionFlags.Present:
                best = a
        return best

    def flat(acts, out):
        for a in acts:
            out.append(a)
            flat(a.children, out)
        return out

    allacts = flat(actions, [])
    log("total actions: {0}".format(len(allacts)))

    # the Present action (or the final draw) carries the swapchain image
    target = None
    for a in reversed(allacts):
        if a.flags & rd.ActionFlags.Present:
            target = a
            break
    if target is None:
        for a in reversed(allacts):
            if a.numIndices > 0:
                target = a
                break

    if target is not None:
        log("using EID {0} ({1})".format(target.eventId, target.GetName(controller.GetStructuredFile())))
        controller.SetFrameEvent(target.eventId, True)
        outs = controller.GetPipelineState().GetOutputTargets()
        for i, o in enumerate(outs):
            rid = getattr(o, "resource", None) or getattr(o, "resourceId", None)
            if rid is None or rid == rd.ResourceId.Null():
                continue
            save_texture(controller, rid,
                         os.path.join(REF_DIR, "final_rt{0}.png".format(i)),
                         "final RT{0}".format(i))

    # also grab the biggest colour target seen on the main hero draw, which is
    # the fully-composited character before UI
    controller.SetFrameEvent(260, True)
    outs = controller.GetPipelineState().GetOutputTargets()
    for i, o in enumerate(outs):
        rid = getattr(o, "resource", None) or getattr(o, "resourceId", None)
        if rid is None or rid == rd.ResourceId.Null():
            continue
        save_texture(controller, rid,
                     os.path.join(REF_DIR, "eid260_rt{0}.png".format(i)),
                     "eid260 RT{0}".format(i))

    # ---------------- 2. orientation sanity check ----------------
    log("")
    log("=" * 60)
    log("ORIENTATION CHECK  (project exported bboxes to pixels)")
    log("=" * 60)
    log("Vulkan NDC is Y-down: bigger view-space Y  ->  LOWER on screen")
    log("")

    ranges = {}
    vr_path = os.path.join(OUT, "vertranges.json")
    if os.path.isfile(vr_path):
        try:
            ranges = json.load(open(vr_path, encoding="utf-8"))
        except Exception as e:
            log("could not read vertranges.json: {0}".format(e))

    # vertranges.json holds OBJECT-space ranges. What we actually want to
    # project is the post-transform (view-space) box, which is exactly what the
    # exported FBX contains - so recompute it from scene.json's transform.
    scene = {}
    sj = r"E:\GST\libai_scene\scene_export\scene.json"
    if os.path.isfile(sj):
        try:
            scene = json.load(open(sj, encoding="utf-8"))
        except Exception as e:
            log("could not read scene.json: {0}".format(e))

    xf_by_eid = {}
    for group, items in (scene.get("groups") or {}).items():
        for it in items:
            xf_by_eid[int(it["eventId"])] = (group, it.get("transform"))

    def apply_xf(m, p):
        """m is row-major 4x4 (as written by export_scene.get_modelview)."""
        if not m or len(m) < 12:
            return p
        x, y, z = p
        return (m[0] * x + m[1] * y + m[2] * z + m[3],
                m[4] * x + m[5] * y + m[6] * z + m[7],
                m[8] * x + m[9] * y + m[10] * z + m[11])

    log("eye at view-space z = {0:.3f}, looking towards +Z".format(EYE_Z))
    log("fovY = {0:.2f} deg, fovX = {1:.2f} deg, aspect = {2:.4f}".format(
        2.0 * __import__("math").degrees(__import__("math").atan(1.0 / PROJ_M11)),
        2.0 * __import__("math").degrees(__import__("math").atan(1.0 / PROJ_M00)),
        PROJ_M11 / PROJ_M00))
    log("")

    with open(os.path.join(OUT, "reference_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))

    # projected bboxes for the record
    proj = {}
    for group, eids in GROUPS.items():
        for eid in eids:
            key = str(eid)
            r = ranges.get(key)
            if not r:
                continue
            mn = r.get("min") or r.get("posMin")
            mx = r.get("max") or r.get("posMax")
            if not mn or not mx:
                continue
            _, xf = xf_by_eid.get(eid, (group, None))
            corners = []
            vmin = [1e30] * 3
            vmax = [-1e30] * 3
            for cx in (mn[0], mx[0]):
                for cy in (mn[1], mx[1]):
                    for cz in (mn[2], mx[2]):
                        vp = apply_xf(xf, (cx, cy, cz))
                        for i in range(3):
                            vmin[i] = min(vmin[i], vp[i])
                            vmax[i] = max(vmax[i], vp[i])
                        p = project(*vp)
                        if p:
                            corners.append(p)
            if not corners:
                continue
            pxs = [c[0] for c in corners]
            pys = [c[1] for c in corners]
            proj[key] = {
                "group": group,
                "viewMin": vmin, "viewMax": vmax,
                "pxMin": [min(pxs), min(pys)],
                "pxMax": [max(pxs), max(pys)],
                "distFromEye": [vmin[2] - EYE_Z, vmax[2] - EYE_Z],
                "hasTransform": bool(xf),
            }
            log("eid{0:<4} {1:<16} viewY {2:6.2f}..{3:6.2f}  ->  screenY {4:6.0f}..{5:6.0f} px   dist {6:6.2f}..{7:6.2f} {8}".format(
                eid, group, vmin[1], vmax[1], min(pys), max(pys),
                vmin[2] - EYE_Z, vmax[2] - EYE_Z,
                "" if xf else "(no xf)"))

    with open(os.path.join(OUT, "projected.json"), "w", encoding="utf-8") as f:
        json.dump(proj, f, indent=1)

    with open(os.path.join(OUT, "reference_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    log("")
    log("wrote reference_log.txt / projected.json")


if __name__ == "__main__":
    import traceback

    if not os.path.isdir(OUT):
        os.makedirs(OUT)

    def _bail(msg):
        LOG.append(msg)
        with open(os.path.join(OUT, "reference_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
        sys.exit(1)

    cap = rd.OpenCaptureFile()
    # NOTE: RenderDoc 1.44 returns a ResultDetails - compare st.code against
    # rd.ResultCode, NOT the long-gone rd.ReplayStatus enum.
    st = cap.OpenFile(RDC, '', None)
    if st.code != rd.ResultCode.Succeeded:
        _bail("FATAL: could not open {0}: {1}".format(RDC, st))
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if st.code != rd.ResultCode.Succeeded:
        cap.Shutdown()
        _bail("FATAL: could not init replay: {0}".format(st))
    try:
        main(controller)
    except Exception as e:
        LOG.append("FATAL {0}".format(e))
        LOG.append(traceback.format_exc())
    finally:
        try:
            controller.Shutdown()
        except Exception:
            pass
        try:
            cap.Shutdown()
        except Exception:
            pass
        with open(os.path.join(OUT, "reference_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
