# -*- coding: utf-8 -*-
"""Smoke-test fbx_ascii_import.py against the real export (Blender background)."""

import os
import sys

sys.path.insert(0, r"E:\GST\libai_scene")

import bpy
import fbx_ascii_import as FA

ROOT = r"E:\GST\libai_scene\scene_export"
LOG = r"E:\GST\libai_scene\out\test_import.txt"

lines = []


def w(msg):
    lines.append(msg)
    print(msg)


def main():
    # start clean
    bpy.ops.wm.read_factory_settings(use_empty=True)

    ok = 0
    fail = 0
    for group in sorted(os.listdir(ROOT)):
        gdir = os.path.join(ROOT, group)
        if not os.path.isdir(gdir) or group.startswith('_'):
            continue
        for fn in sorted(os.listdir(gdir)):
            if not fn.lower().endswith('.fbx'):
                continue
            path = os.path.join(gdir, fn)
            try:
                obj = FA.load_ascii_fbx(path)
            except Exception as e:
                w("FAIL {0}/{1}: {2}".format(group, fn, e))
                fail += 1
                continue
            if obj is None:
                w("FAIL {0}/{1}: builder returned None".format(group, fn))
                fail += 1
                continue
            bpy.context.scene.collection.objects.link(obj)
            me = obj.data
            xs = [v.co.x for v in me.vertices]
            ys = [v.co.y for v in me.vertices]
            zs = [v.co.z for v in me.vertices]
            uvnames = [l.name for l in me.uv_layers]
            # sanity: UVs should mostly sit in a sane range
            uvmin = uvmax = None
            if me.uv_layers:
                us = [d.uv[0] for d in me.uv_layers[0].data]
                vs = [d.uv[1] for d in me.uv_layers[0].data]
                if us:
                    uvmin = (min(us), min(vs))
                    uvmax = (max(us), max(vs))
            w("OK {0}/{1}: v={2} tri={3} uv={4} bbox=({5:.2f},{6:.2f},{7:.2f})-({8:.2f},{9:.2f},{10:.2f}) uv0={11}..{12}".format(
                group, fn, len(me.vertices), len(me.polygons), uvnames,
                min(xs), min(ys), min(zs), max(xs), max(ys), max(zs),
                uvmin, uvmax))
            ok += 1

    w("")
    w("SUMMARY: ok={0} fail={1}".format(ok, fail))

    outdir = os.path.dirname(LOG)
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    with open(LOG, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))


main()
