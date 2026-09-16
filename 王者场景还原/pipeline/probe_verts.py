# -*- coding: utf-8 -*-
"""
Probe raw vertex position ranges (object space) + view matrix decomposition.

Run:
    qTinecmaTool.exe --python E:\\GST\\libai_scene\\probe_verts.py

Answers two questions:
 1. Are the raw POSITION values object-space (each mesh centred near origin,
    small extents) or already world-space (spread across the scene)?
 2. Given the per-draw model-view matrix candidates, what does the geometry
    look like AFTER applying each, so we can pick the right one.
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import json
import struct
import traceback

import renderdoc as rd

RDC_PATH = r"E:\GST\libai_scene\libai.rdc"
OUT_DIR = r"E:\GST\libai_scene\out"
PROBE_EIDS = [260, 279, 562, 692, 274, 253, 246, 647, 267, 365, 353, 601]


class MeshData(rd.MeshFormat):
    indexOffset = 0
    name = ''


def unpack_pos(fmt, data):
    """Minimal float3 unpack for the POSITION attribute."""
    if fmt.Special():
        return None
    n = int(fmt.compCount)
    bw = int(fmt.compByteWidth)
    ct = fmt.compType
    if ct == rd.CompType.Float and bw == 4:
        code = str(n) + 'f'
    elif ct == rd.CompType.Float and bw == 2:
        code = str(n) + 'e'
    else:
        return None
    try:
        return struct.unpack_from(code, data, 0)
    except Exception:
        return None


def get_inputs(controller, draw):
    state = controller.GetPipelineState()
    ib = state.GetIBuffer()
    vbs = state.GetVBuffers()
    attrs = state.GetVertexInputs()
    out = []
    for attr in attrs:
        if attr.perInstance:
            continue
        mi = MeshData()
        mi.indexResourceId = ib.resourceId
        mi.indexByteOffset = ib.byteOffset
        mi.indexByteStride = ib.byteStride
        mi.baseVertex = draw.baseVertex
        mi.indexOffset = draw.indexOffset
        mi.numIndices = draw.numIndices
        if not (draw.flags & rd.ActionFlags.Indexed):
            mi.indexResourceId = rd.ResourceId.Null()
        vb = vbs[attr.vertexBuffer]
        mi.vertexByteOffset = attr.byteOffset + vb.byteOffset + draw.vertexOffset * vb.byteStride
        mi.format = attr.format
        mi.vertexResourceId = vb.resourceId
        mi.vertexByteStride = vb.byteStride
        mi.name = attr.name
        out.append(mi)
    return out


def get_indices(controller, mesh):
    if mesh.indexByteStride == 2:
        code = 'H'
    elif mesh.indexByteStride == 4:
        code = 'I'
    else:
        code = 'B'
    if mesh.indexResourceId != rd.ResourceId.Null():
        ibdata = controller.GetBufferData(mesh.indexResourceId, mesh.indexByteOffset, 0)
        off = mesh.indexOffset * mesh.indexByteStride
        idx = struct.unpack_from(str(mesh.numIndices) + code, ibdata, off)
        return [i + mesh.baseVertex for i in idx]
    return list(range(mesh.numIndices))


def find_action(controller, eid):
    res = [None]

    def rec(actions):
        for a in actions:
            if a.eventId == eid:
                res[0] = a
                return True
            if a.children and rec(a.children):
                return True
        return False
    rec(controller.GetRootActions())
    return res[0]


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

    out = {}
    try:
        for eid in PROBE_EIDS:
            draw = find_action(controller, eid)
            if draw is None:
                log("EID {0} not found".format(eid))
                continue
            controller.SetFrameEvent(eid, True)
            mis = get_inputs(controller, draw)
            if not mis:
                log("EID {0}: no inputs".format(eid))
                continue

            # POSITION = first float attr with >=3 comps
            pos_mi = None
            for mi in mis:
                if mi.format.Special():
                    continue
                if int(mi.format.compCount) >= 3 and mi.format.compType == rd.CompType.Float:
                    pos_mi = mi
                    break
            if pos_mi is None:
                log("EID {0}: no float3 position".format(eid))
                continue

            idx = get_indices(controller, mis[0])
            uniq = sorted(set(idx))
            maxi = max(uniq) if uniq else 0
            size = (maxi + 1) * pos_mi.vertexByteStride
            buf = controller.GetBufferData(pos_mi.vertexResourceId, pos_mi.vertexByteOffset, size)

            mn = [1e30] * 3
            mx = [-1e30] * 3
            cnt = 0
            for i in uniq:
                off = i * pos_mi.vertexByteStride
                v = unpack_pos(pos_mi.format, buf[off:off + 32])
                if not v:
                    continue
                cnt += 1
                for c in range(3):
                    if v[c] < mn[c]:
                        mn[c] = v[c]
                    if v[c] > mx[c]:
                        mx[c] = v[c]
            if cnt == 0:
                continue
            ctr = [(mn[c] + mx[c]) / 2.0 for c in range(3)]
            ext = [mx[c] - mn[c] for c in range(3)]
            out[str(eid)] = {"attr": pos_mi.name, "verts": cnt,
                             "min": mn, "max": mx, "center": ctr, "extent": ext}
            log("EID {0:4d} attr={1:8s} verts={2:6d}".format(eid, str(pos_mi.name), cnt))
            log("     min=({0:9.3f},{1:9.3f},{2:9.3f})  max=({3:9.3f},{4:9.3f},{5:9.3f})".format(
                mn[0], mn[1], mn[2], mx[0], mx[1], mx[2]))
            log("     center=({0:8.3f},{1:8.3f},{2:8.3f})  extent=({3:8.3f},{4:8.3f},{5:8.3f})".format(
                ctr[0], ctr[1], ctr[2], ext[0], ext[1], ext[2]))

        with open(os.path.join(OUT_DIR, "vertranges.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        log("")
        log("wrote vertranges.json")
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
            with open(os.path.join(OUT_DIR, "vertranges_log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass


if __name__ == "__main__":
    main()
    sys.exit(0)
