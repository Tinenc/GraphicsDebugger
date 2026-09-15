# -*- coding: utf-8 -*-
import os, sys, traceback
import renderdoc as rd

RDC = r"E:\GST\白金猎豹\兰博-金属风暴.rdc"
EVENT = 427
OUT = r"E:\GST\白金猎豹\carbody_export\ao_analysis\427_tex"
os.makedirs(OUT, exist_ok=True)

LOGF = open(os.path.join(OUT, "_dump_log.txt"), "w", encoding="utf-8")
def log(m):
    LOGF.write(str(m) + "\n"); LOGF.flush()

try:
    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(RDC, "", None)
    log("OpenFile code={0}".format(str(st.code)))
    status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    log("OpenCapture code={0}".format(str(status.code)))

    controller.SetFrameEvent(EVENT, True)
    state = controller.GetPipelineState()

    texmap = {}
    for t in controller.GetTextures():
        texmap[int(t.resourceId)] = t
    log("total textures: {0}".format(len(texmap)))

    used = state.GetReadOnlyResources(rd.ShaderStage.Fragment)
    log("=== PS read-only resources at event {0} (count={1}) ===".format(EVENT, len(used)))
    seen = set()
    idx = 0
    for ud in used:
        try:
            res = ud.descriptor.resource
        except Exception as e:
            log("  [bind {0}] descriptor err {1}".format(idx, str(e))); idx += 1; continue
        rid = int(res)
        if res == rd.ResourceId.Null():
            log("  [bind {0}] NULL".format(idx)); idx += 1; continue
        t = texmap.get(rid)
        if t is None:
            log("  [bind {0}] res {1} (no tex desc, likely buffer)".format(idx, rid)); idx += 1; continue
        fmt = t.format.Name()
        cube = getattr(t, "cubemap", False)
        log("  [bind {0}] res {1}: {2}x{3} arr{4} mips{5} cube={6} fmt={7}".format(
            idx, rid, t.width, t.height, t.arraysize, t.mips, cube, fmt))
        if rid not in seen:
            seen.add(rid)
            ts = rd.TextureSave()
            ts.resourceId = res
            ts.mip = 0
            ts.slice.sliceIndex = 0
            ts.alpha = rd.AlphaMapping.Preserve
            ts.destType = rd.FileType.PNG
            out = os.path.join(OUT, "tex_{0}.png".format(rid))
            try:
                controller.SaveTexture(ts, out)
                log("      saved {0}".format(out))
            except Exception as e:
                log("      save fail: {0}".format(str(e)))
        idx += 1

    controller.Shutdown()
    cap.Shutdown()
    log("DONE")
except Exception:
    log("EXC:\n" + traceback.format_exc())
finally:
    LOGF.close()

sys.exit(0)
