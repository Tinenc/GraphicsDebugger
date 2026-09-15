# -*- coding: utf-8 -*-
import json, os
ROOT = r"E:\GST\白金猎豹\carbody_export"

def load(s):
    with open(os.path.join(ROOT, s, "uv_dump.json"), "r", encoding="utf-8") as f:
        return json.load(f)

for skin in ["原皮", "金属风暴", "赛博超域"]:
    d = load(skin)
    print("===", skin, "===")
    # find vertex color attr = COLOR mapper, or _input13
    color_attr = d["mapper"].get("COLOR", "") or "_input13"
    if color_attr not in d["rawAttrs"]:
        print("  no color attr", color_attr); continue
    rows = d["rawAttrs"][color_attr]
    comps = max(len(r) for r in rows)
    print("  color attr:", color_attr, "comps:", comps, "verts:", len(rows))
    for c in range(comps):
        col = [r[c] for r in rows if c < len(r)]
        mn, mx = min(col), max(col)
        mean = sum(col)/len(col)
        var = sum((x-mean)**2 for x in col)/len(col)
        # histogram-ish: fraction near 0 and near 1
        near0 = sum(1 for x in col if x < 0.05)/len(col)
        near1 = sum(1 for x in col if x > 0.95)/len(col)
        print("   comp{0}: min={1:.4f} max={2:.4f} mean={3:.4f} std={4:.4f} near0={5:.1%} near1={6:.1%}".format(
            c, mn, mx, mean, var**0.5, near0, near1))
    print("   first5:", [ [round(v,3) for v in r] for r in rows[:5] ])
