# -*- coding: utf-8 -*-
"""
Headless frame survey for RenderDoc / TinecmaTool (Python 3.6 embedded).

Run:
    qTinecmaTool.exe --python E:\\GST\\libai_scene\\scene_survey.py

Goal: enumerate every draw call in a capture and record enough signal to
classify it (mesh asset vs UI vs fullscreen post pass), so we can pick the
EventIDs worth exporting to FBX.

Writes:
    OUT_DIR/survey.json   machine-readable, one record per draw
    OUT_DIR/survey.txt    human-readable table
    OUT_DIR/survey_log.txt
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import json
import traceback

import renderdoc as rd

# ==================== CONFIG ====================

RDC_PATH = r"E:\GST\libai_scene\libai.rdc"
OUT_DIR = r"E:\GST\libai_scene\out"

# ==================== helpers ====================


def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def collect_actions(controller):
    """Flatten the action tree, keeping only real draws."""
    out = []

    def rec(actions, depth, marker_path):
        for a in actions:
            name = ""
            try:
                name = a.GetName(controller.GetStructuredFile())
            except Exception:
                try:
                    name = str(a.customName)
                except Exception:
                    name = ""
            kids = []
            try:
                kids = list(a.children) if a.children else []
            except Exception:
                kids = []

            is_draw = False
            try:
                is_draw = bool(a.flags & rd.ActionFlags.Drawcall)
            except Exception:
                is_draw = safe_int(getattr(a, "numIndices", 0)) > 0

            if is_draw:
                out.append({
                    "eventId": safe_int(a.eventId),
                    "name": name,
                    "numIndices": safe_int(getattr(a, "numIndices", 0)),
                    "numInstances": safe_int(getattr(a, "numInstances", 1)),
                    "markerPath": " / ".join(marker_path),
                    "depth": depth,
                })

            if kids:
                nextpath = marker_path
                if not is_draw and name:
                    nextpath = marker_path + [name]
                rec(kids, depth + 1, nextpath)

    rec(controller.GetRootActions(), 0, [])
    return out


def describe_draw(controller, rec, log):
    """Enrich one draw record with pipeline / binding / vertex-format info."""
    eid = rec["eventId"]
    try:
        controller.SetFrameEvent(eid, True)
    except Exception as e:
        rec["error"] = "SetFrameEvent: {0}".format(e)
        return rec

    try:
        state = controller.GetPipelineState()
    except Exception as e:
        rec["error"] = "GetPipelineState: {0}".format(e)
        return rec

    # --- vertex input attribute formats (multi-UV signal) ---
    attrs = []
    try:
        vin = state.GetVertexInputs()
        vbs = state.GetVBuffers()
        for a in vin:
            try:
                if a.perInstance:
                    continue
                vbidx = safe_int(a.vertexBuffer, -1)
                stride = 0
                if 0 <= vbidx < len(vbs):
                    stride = safe_int(vbs[vbidx].byteStride)
                special = False
                try:
                    special = bool(a.format.Special())
                except Exception:
                    special = False
                attrs.append({
                    "name": str(a.name),
                    "comp": safe_int(a.format.compCount),
                    "type": str(a.format.compType).replace("CompType.", ""),
                    "byteWidth": safe_int(a.format.compByteWidth),
                    "vb": vbidx,
                    "stride": stride,
                    "special": special,
                })
            except Exception as e:
                attrs.append({"name": "<err>", "err": str(e)})
    except Exception as e:
        rec["attrError"] = str(e)
    rec["attrs"] = attrs
    rec["numAttrs"] = len([a for a in attrs if not a.get("special") and "err" not in a])
    # float attributes are the multi-UV carriers (see exporter infer_mapper)
    rec["numFloatAttrs"] = len([a for a in attrs
                                if a.get("type") == "Float" and not a.get("special")])

    # --- bound fragment textures ---
    texs = []
    try:
        used = state.GetReadOnlyResources(rd.ShaderStage.Fragment)
        seen = set()
        for ud in used:
            try:
                res = ud.descriptor.resource
            except Exception:
                continue
            if res == rd.ResourceId.Null():
                continue
            rid = safe_int(res)
            if rid in seen:
                continue
            seen.add(rid)
            entry = {"id": rid}
            try:
                t = controller.GetTexture(res)
                if t is not None:
                    entry["w"] = safe_int(t.width)
                    entry["h"] = safe_int(t.height)
                    entry["fmt"] = str(t.format.Name())
            except Exception:
                pass
            texs.append(entry)
    except Exception as e:
        rec["texError"] = str(e)
    rec["textures"] = texs
    rec["numTextures"] = len(texs)

    # --- render target / viewport ---
    try:
        rts = state.GetOutputTargets()
        rec["numRT"] = len(rts)
        r0 = None
        for cand in rts:
            got = None
            for path in ("resource", "resourceId"):
                try:
                    got = getattr(cand, path)
                    break
                except Exception:
                    continue
            if got is None:
                try:
                    got = cand.descriptor.resource
                except Exception:
                    got = None
            if got is not None and safe_int(got) != 0:
                r0 = got
                break
        if r0 is not None:
            rec["rt0"] = safe_int(r0)
            try:
                t = controller.GetTexture(r0)
                if t is not None:
                    rec["rtW"] = safe_int(t.width)
                    rec["rtH"] = safe_int(t.height)
                    rec["rtFmt"] = str(t.format.Name())
            except Exception as e:
                rec["rtError"] = str(e)
    except Exception as e:
        rec["rtError"] = str(e)

    try:
        vps = state.GetViewport(0)
        rec["vpW"] = round(float(vps.width), 1)
        rec["vpH"] = round(float(vps.height), 1)
    except Exception:
        pass

    # --- depth state: real geometry usually writes/tests depth ---
    try:
        ds = state.GetDepthTarget()
        rec["hasDepth"] = safe_int(ds.resource) != 0
    except Exception:
        rec["hasDepth"] = None

    return rec


def classify(rec):
    """Heuristic bucket for the draw."""
    ni = rec.get("numIndices", 0)
    nt = rec.get("numTextures", 0)
    na = rec.get("numAttrs", 0)
    vpw = rec.get("vpW", 0) or 0
    vph = rec.get("vpH", 0) or 0

    # 1024x1024 square viewport with no textures = shadow map depth pass
    if nt == 0 and abs(vpw - vph) < 1.0 and vpw > 0:
        return "shadow_pass"
    # fullscreen blit / post process: 3-6 verts covering the screen
    if ni <= 6:
        return "fullscreen_or_post"
    if ni < 24 and nt <= 1:
        return "tiny_or_ui"
    if ni >= 300 and na >= 3:
        return "mesh_asset"
    if ni >= 24:
        return "small_mesh"
    return "unknown"


def attr_summary(rec):
    """Compact vertex-layout string, e.g. '_input0:3xFloat|_input5:4xFloat'."""
    parts = []
    for a in rec.get("attrs", []):
        if a.get("special") or "err" in a:
            continue
        parts.append("{0}:{1}x{2}".format(a.get("name", "?"),
                                          a.get("comp", 0),
                                          a.get("type", "?")))
    return "|".join(parts)


def main():
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except Exception:
        pass

    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(str(msg))

    def flush_log():
        try:
            with open(os.path.join(OUT_DIR, "survey_log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(log_lines))
        except Exception:
            pass

    log("survey: opening {0}".format(RDC_PATH))
    if not os.path.isfile(RDC_PATH):
        log("FATAL: rdc not found")
        flush_log()
        return

    cap = rd.OpenCaptureFile()
    status = cap.OpenFile(RDC_PATH, '', None)
    if status.code != rd.ResultCode.Succeeded:
        log("FATAL: OpenFile failed: {0}".format(status.code))
        flush_log()
        return
    if cap.LocalReplaySupport() != rd.ReplaySupport.Supported:
        log("FATAL: local replay not supported")
        cap.Shutdown()
        flush_log()
        return

    status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if status.code != rd.ResultCode.Succeeded:
        log("FATAL: OpenCapture failed: {0}".format(status.code))
        cap.Shutdown()
        flush_log()
        return

    try:
        api = ""
        try:
            api = str(cap.DriverName())
        except Exception:
            pass
        log("api={0}".format(api))

        draws = collect_actions(controller)
        log("draw calls found: {0}".format(len(draws)))

        for i, rec in enumerate(draws):
            try:
                describe_draw(controller, rec, log)
            except Exception as e:
                rec["error"] = str(e)
            rec["bucket"] = classify(rec)
            if (i + 1) % 25 == 0:
                log("  ...{0}/{1}".format(i + 1, len(draws)))

        payload = {
            "rdc": RDC_PATH,
            "api": api,
            "drawCount": len(draws),
            "draws": draws,
        }
        with open(os.path.join(OUT_DIR, "survey.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

        # human readable, sorted by index count desc
        lines = []
        lines.append("api={0}  draws={1}".format(api, len(draws)))
        lines.append("")
        hdr = "{0:>6} {1:>8} {2:>5} {3:>4} {4:>5} {5:>4} {6:>11} {7:>13}  {8:<19} {9}".format(
            "EID", "indices", "inst", "attr", "fattr", "tex", "rtWxH", "vpWxH", "bucket", "layout")
        lines.append(hdr)
        lines.append("-" * len(hdr))
        for rec in sorted(draws, key=lambda r: -r.get("numIndices", 0)):
            lines.append("{0:>6} {1:>8} {2:>5} {3:>4} {4:>5} {5:>4} {6:>11} {7:>13}  {8:<19} {9}".format(
                rec.get("eventId", 0),
                rec.get("numIndices", 0),
                rec.get("numInstances", 1),
                rec.get("numAttrs", 0),
                rec.get("numFloatAttrs", 0),
                rec.get("numTextures", 0),
                "{0}x{1}".format(rec.get("rtW", "?"), rec.get("rtH", "?")),
                "{0}x{1}".format(rec.get("vpW", "?"), rec.get("vpH", "?")),
                rec.get("bucket", ""),
                attr_summary(rec)[:100],
            ))

        # bucket summary
        lines.append("")
        lines.append("bucket summary:")
        buckets = {}
        for rec in draws:
            b = rec.get("bucket", "")
            buckets[b] = buckets.get(b, 0) + 1
        for b in sorted(buckets, key=lambda k: -buckets[k]):
            lines.append("  {0:<22} {1}".format(b, buckets[b]))

        with open(os.path.join(OUT_DIR, "survey.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        log("wrote survey.json / survey.txt")
    except Exception as e:
        log("FATAL: {0}".format(e))
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
        flush_log()


if __name__ == "__main__":
    main()
    sys.exit(0)
