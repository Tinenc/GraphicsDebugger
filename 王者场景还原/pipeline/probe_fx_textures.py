"""Why do the 03_props FX ribbons have no textures?

The manifest recorded nothing for eid365/353/334/322/338/377/407/389/423/307.
Check whether they genuinely bind no fragment textures, or whether the
exporter's GetReadOnlyResources() path missed them (e.g. the shader samples
via a different stage/binding, or they are pure vertex-colour / gradient FX).

Run through the headless channel:
  qTinecmaTool.exe --python probe_fx_textures.py
"""
import os
import sys

import renderdoc as rd

RDC_PATH = r"E:\GST\libai_scene\libai.rdc"
OUT = r"E:\GST\libai_scene\out\fx_textures.txt"

FX_EIDS = [365, 353, 334, 322, 338, 377, 407, 389, 423, 307]
# a known-good one for comparison (hero body, has basecolor+normal)
REF_EIDS = [260]

_lines = []


def log(m):
    _lines.append(str(m))
    print(str(m))


def flush():
    try:
        d = os.path.dirname(OUT)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines) + "\n")
    except Exception as e:
        print("could not write report: {0}".format(e))


def find_action(controller, eid):
    def rec(actions):
        for a in actions:
            if a.eventId == eid:
                return a
            got = rec(a.children)
            if got is not None:
                return got
        return None
    return rec(controller.GetRootActions())


def dump_stage_resources(controller, state, stage, label):
    """List every read-only resource bound to a shader stage."""
    try:
        used = state.GetReadOnlyResources(stage)
    except Exception as e:
        log("    {0}: enumerate failed: {1}".format(label, e))
        return 0
    n = 0
    for slot, ud in enumerate(used):
        try:
            res = ud.descriptor.resource
        except Exception:
            continue
        if res == rd.ResourceId.Null():
            continue
        n += 1
        info = "slot{0} id={1}".format(slot, int(res))
        try:
            t = controller.GetTexture(res)
            if t is not None:
                info += " tex {0}x{1} mips={2} fmt={3}".format(
                    t.width, t.height, t.mips, t.format.Name())
            else:
                info += " (not a texture -- buffer?)"
        except Exception as e:
            info += " GetTexture failed: {0}".format(e)
        log("    {0}: {1}".format(label, info))
    if n == 0:
        log("    {0}: NONE".format(label))
    return n


def probe(controller, eid):
    log("")
    log("=========== eid {0} ===========".format(eid))
    act = find_action(controller, eid)
    if act is None:
        log("  action not found")
        return
    controller.SetFrameEvent(eid, True)
    log("  name: {0}".format(act.GetName(controller.GetStructuredFile())))
    log("  numIndices={0} numInstances={1}".format(
        act.numIndices, act.numInstances))

    state = controller.GetPipelineState()

    total = 0
    for stage, label in ((rd.ShaderStage.Fragment, "FS"),
                         (rd.ShaderStage.Vertex, "VS")):
        total += dump_stage_resources(controller, state, stage, label)

    # what does the fragment shader actually declare it wants?
    try:
        refl = state.GetShaderReflection(rd.ShaderStage.Fragment)
        if refl is not None:
            ros = getattr(refl, "readOnlyResources", [])
            log("  FS reflection declares {0} read-only resource(s):".format(
                len(ros)))
            for r in ros:
                log("      name={0!r} type={1}".format(
                    getattr(r, "name", "?"), getattr(r, "textureType", "?")))
            sams = getattr(refl, "samplers", [])
            log("  FS declares {0} sampler(s)".format(len(sams)))
        else:
            log("  FS reflection: None")
    except Exception as e:
        log("  FS reflection failed: {0}".format(e))

    # vertex inputs -- colour-driven FX would carry a COLOR attribute
    try:
        vs_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if vs_refl is not None:
            sig = [getattr(s, "varName", None) or getattr(s, "semanticName", "?")
                   for s in vs_refl.inputSignature]
            log("  VS input signature: {0}".format(sig))
    except Exception as e:
        log("  VS signature failed: {0}".format(e))

    # blend state tells us if this is additive FX
    try:
        blends = state.GetColorBlends()
        if blends:
            b = blends[0]
            log("  blend[0]: enabled={0} src={1} dst={2} op={3}".format(
                b.enabled, b.colorBlend.source, b.colorBlend.destination,
                b.colorBlend.operation))
    except Exception as e:
        log("  blend query failed: {0}".format(e))

    log("  --> total bound read-only resources: {0}".format(total))


def main():
    log("rdc: {0}".format(RDC_PATH))
    if not os.path.isfile(RDC_PATH):
        log("FATAL: capture not found")
        flush()
        return

    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(RDC_PATH, '', None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenFile {0}".format(st.code))
        flush()
        return
    st, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if st.code != rd.ResultCode.Succeeded:
        log("FATAL OpenCapture {0}".format(st.code))
        flush()
        return

    try:
        for eid in REF_EIDS:
            log("")
            log("### REFERENCE (known to have textures) ###")
            probe(controller, eid)
        for eid in FX_EIDS:
            probe(controller, eid)
    finally:
        controller.Shutdown()
        cap.Shutdown()
        flush()
        log("report -> {0}".format(OUT))


main()
