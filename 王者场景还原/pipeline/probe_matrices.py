# -*- coding: utf-8 -*-
"""
Probe vertex-shader constant buffers for candidate transform matrices.

Run:
    qTinecmaTool.exe --python E:\\GST\\libai_scene\\probe_matrices.py

For each surveyed draw we dump EVERY 4x4 float window in the VS constant
blocks that looks affine (last column ~ 0,0,0,1 in either row- or
column-major reading), plus the variable name. Goal: figure out which CB
variable is the per-object WORLD matrix vs the shared VIEW/PROJ matrix.

Key discriminator: a VIEW or PROJ matrix is IDENTICAL across all draws;
a WORLD matrix DIFFERS per object. So we compare matrices across draws.
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

# Draws to probe: the 7 big shadow-casting assets + a few mid ones.
PROBE_EIDS = [260, 279, 562, 692, 274, 253, 246, 647, 267, 365, 353, 334, 601]


def looks_affine_rowmajor(m):
    """m[3],m[7],m[11]==0 and m[15]==1  (translation in 4th column)."""
    return (abs(m[3]) < 1e-6 and abs(m[7]) < 1e-6 and
            abs(m[11]) < 1e-6 and abs(m[15] - 1.0) < 1e-6)


def looks_affine_colmajor(m):
    """m[12],m[13],m[14] = translation, m[3],m[7],m[11]=0, m[15]=1
    i.e. the last ROW is 0,0,0,1 -> that's rowmajor test on transpose."""
    return (abs(m[12]) < 1e-6 and abs(m[13]) < 1e-6 and
            abs(m[14]) < 1e-6 and abs(m[15] - 1.0) < 1e-6)


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
        log("FATAL OpenFile {0}".format(st.code))
        return
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenCapture {0}".format(st.code))
        cap.Shutdown()
        return

    results = {}
    try:
        for eid in PROBE_EIDS:
            try:
                controller.SetFrameEvent(eid, True)
            except Exception as e:
                log("EID {0}: SetFrameEvent failed {1}".format(eid, e))
                continue

            state = controller.GetPipelineState()
            refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
            if not refl:
                log("EID {0}: no VS reflection".format(eid))
                continue

            runtime_cbs = state.GetConstantBlocks(rd.ShaderStage.Vertex)
            found = []

            for cb_index, cb_refl in enumerate(refl.constantBlocks):
                cb_name = ""
                try:
                    cb_name = str(cb_refl.name)
                except Exception:
                    pass
                try:
                    cb_set = cb_refl.fixedBindSetOrSpace
                    cb_binding = cb_refl.fixedBindNumber
                except Exception:
                    cb_set = cb_binding = -1

                runtime_cb = runtime_cbs[cb_index] if cb_index < len(runtime_cbs) else None
                if runtime_cb is None:
                    continue
                try:
                    desc = runtime_cb.descriptor
                    res_id = desc.resource
                    off = getattr(desc, "byteOffset", 0)
                    size = getattr(desc, "byteSize", cb_refl.byteSize)
                    buf = controller.GetBufferData(res_id, off, size)
                except Exception as e:
                    continue

                for var in cb_refl.variables:
                    try:
                        vname = str(var.name)
                    except Exception:
                        continue
                    voff = getattr(var, "byteOffset", getattr(var, "offset", 0))

                    # variable may be an array of matrices; walk 64-byte windows
                    try:
                        vsize = int(var.type.descriptor.arrayByteStride) or 64
                    except Exception:
                        vsize = 64
                    try:
                        rows = int(var.type.descriptor.rows)
                        cols = int(var.type.descriptor.columns)
                    except Exception:
                        rows = cols = 0

                    if len(buf) < voff + 64:
                        continue
                    try:
                        m = struct.unpack_from('16f', buf, voff)
                    except Exception:
                        continue

                    ar = looks_affine_rowmajor(m)
                    ac = looks_affine_colmajor(m)
                    if not (ar or ac):
                        continue
                    found.append({
                        "cb": cb_name,
                        "cbIdx": cb_index,
                        "set": int(cb_set),
                        "binding": int(cb_binding),
                        "var": vname,
                        "off": int(voff),
                        "rows": rows,
                        "cols": cols,
                        "rowMajorAffine": ar,
                        "colMajorAffine": ac,
                        "m": [round(float(x), 6) for x in m],
                    })

            results[str(eid)] = found
            log("EID {0}: {1} affine candidate(s)".format(eid, len(found)))
            for f in found:
                m = f["m"]
                tag = "ROW" if f["rowMajorAffine"] else "COL"
                trans = (m[3], m[7], m[11]) if f["rowMajorAffine"] else (m[12], m[13], m[14])
                log("   [{0}] cb='{1}'(set{2}/bind{3}) var='{4}'@{5} {6}x{7} trans=({8:.3f},{9:.3f},{10:.3f})".format(
                    tag, f["cb"], f["set"], f["binding"], f["var"], f["off"],
                    f["rows"], f["cols"], trans[0], trans[1], trans[2]))

        with open(os.path.join(OUT_DIR, "matrices.json"), "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        log("")
        log("wrote matrices.json")
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
            with open(os.path.join(OUT_DIR, "matrices_log.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
        except Exception:
            pass


if __name__ == "__main__":
    main()
    sys.exit(0)
