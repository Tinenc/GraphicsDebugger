# -*- coding: utf-8 -*-
"""Dump blend state for the newly-imported scene-background draws.

Run: qTinecmaTool.exe --python E:\\GST\\libai_scene\\probe_blend.py

Writes E:\\GST\\libai_scene\\out\\blend_state.txt line by line, then
sys.exit(0) so the GUI does not stay open.
"""
from __future__ import print_function

import os
import sys
import traceback

OUT = r"E:\GST\libai_scene\out\blend_state.txt"
lines = []


def log(m):
    lines.append(str(m))
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


EIDS = [
    # 05_backdrop
    231, 238, 449,
    # 06_fx_extra
    391, 411, 596, 619,
    # 07_scene_detail (44)
    313, 344, 397, 398, 463, 470, 477, 484, 491, 498, 505,
    512, 516, 523, 530, 537, 544, 548, 555, 576, 583, 589,
    608, 612, 626, 633, 640, 654, 655, 656, 663, 670, 677,
    685, 712, 726, 730, 732, 736, 740, 744, 750, 754, 757,
]

try:
    import renderdoc as rd
    log("renderdoc imported")
    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(r"E:\GST\libai_scene\libai.rdc", '', None)
    log("OpenFile: " + str(st.code))
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    log("OpenCapture: " + str(st.code))

    # build action index
    index = {}

    def walk(a):
        if getattr(a, 'numIndices', 0) or getattr(a, 'numInstances', 0):
            index[a.eventId] = a
        for c in a.children:
            walk(c)

    for r in controller.GetRootActions():
        walk(r)
    log("actions indexed: {0}".format(len(index)))

    first = True
    for eid in EIDS:
        draw = index.get(eid)
        if draw is None:
            log("EID {0}: NOT FOUND".format(eid))
            continue
        try:
            controller.SetFrameEvent(eid, True)
            ps = controller.GetPipelineState()
            cbs_list = ps.GetColorBlends()
            if not cbs_list:
                log("EID {0}: no color blend".format(eid))
                continue
            cbs = cbs_list[0]
            if first:
                first = False
                log("colorBlend attrs: {0}".format(
                    sorted(a for a in dir(cbs.colorBlend) if not a.startswith("_"))))
                log("alphaBlend attrs: {0}".format(
                    sorted(a for a in dir(cbs.alphaBlend) if not a.startswith("_"))))

            def g(f, *names):
                for n in names:
                    v = getattr(f, n, None)
                    if v is not None:
                        return v
                return "?"

            cb = cbs.colorBlend
            ab = cbs.alphaBlend
            log("EID {0}: en={1} color[{2}/{3}] alpha[{4}/{5}]".format(
                eid, bool(cbs.enabled),
                g(cb, "source", "blendSource"), g(cb, "destination", "blendDestination"),
                g(ab, "source", "blendSource"), g(ab, "destination", "blendDestination")))
        except Exception as e:
            log("EID {0}: ERR {1}".format(eid, e))
    controller.Shutdown()
    cap.Shutdown()
    log("DONE")
except Exception as e:
    log("FATAL {0}".format(e))
    log(traceback.format_exc())

sys.exit(0)
